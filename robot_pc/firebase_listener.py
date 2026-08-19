"""
Firebase listener for the ViperX300 arm (Option B).

WHAT THIS FILE DOES
-------------------
The Flutter app never talks to this PC directly. Instead, the app writes a
"command" document into the `RobotCommands` collection in Cloud Firestore.

This script logs into the SAME Firebase project using the Admin SDK, keeps an
open connection to Firestore, and reacts the instant a new command appears:

    1. A caregiver taps "Pick Up" in the app.
    2. The app creates  RobotCommands/<id>  with  status = "pending".
    3. THIS script sees the new document in real time.
    4. It "claims" the document (status -> "in_progress") so no other machine
       runs the same command twice.
    5. It calls the exact same robot functions the HTTP backend used
       (pick_up / shelf_meds / check_shelf).
    6. It writes the outcome back to the same document
       (status -> "success" or "failed", plus finishedAt / errorMessage).
    7. The app, which is listening to that document, updates the UI instantly.

Because this script only makes OUTBOUND connections to Firebase, the lab network
does not need any open/forwarded ports. That is the whole point of Option B.

HOW TO RUN
----------
    pip install -r requirements.txt
    # put your service account key next to this file as serviceAccountKey.json
    python firebase_listener.py

See robot_pc/README.md for the full, beginner-friendly setup guide.
"""

import contextlib
import logging
import os
import queue
import sys
import threading
import time

import firebase_admin
from firebase_admin import credentials, firestore

# ---------------------------------------------------------------------------
# Configuration (edit these if your paths differ)
# ---------------------------------------------------------------------------

# Path to the Firebase service account key (downloaded from the Firebase
# console). You can also set the environment variable
# GOOGLE_APPLICATION_CREDENTIALS instead of using this file.
SERVICE_ACCOUNT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "serviceAccountKey.json"
)

# Name of the Firestore collection that holds the command queue.
COLLECTION_NAME = "RobotCommands"

# If you deploy the optional Cloud Function, it validates each command and moves
# it from "pending" to "queued". In that case set this to True so the robot only
# executes commands that were already validated. If you are NOT using the Cloud
# Function, leave it False and the robot will execute "pending" commands itself.
REQUIRE_VALIDATION = False

# Set to True to test the whole pipeline WITHOUT a real arm connected. The robot
# functions are replaced by short sleeps that pretend to work.
FORCE_MOCK_MODE = False

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("robot_listener")


# ---------------------------------------------------------------------------
# Import the real robot functions (same ones the HTTP backend used)
# ---------------------------------------------------------------------------

# The robot control scripts live in the "Sem1 2024-25 ..." folder one level up.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_PATH = os.path.join(
    PROJECT_ROOT, "Sem1 2024-25 - VLN Based Medicine Management System"
)
sys.path.append(SCRIPTS_PATH)

if FORCE_MOCK_MODE:
    MOCK_MODE = True
else:
    try:
        from check_shelf import check_shelf
        from shelve_meds import shelf_meds
        from robot_func2 import pick_up, get_position

        MOCK_MODE = False
    except ImportError as exc:
        logger.warning("Could not import robot modules (%s).", exc)
        logger.warning("Running in MOCK MODE - no real arm movement will happen.")
        MOCK_MODE = True


@contextlib.contextmanager
def change_dir(path):
    """Temporarily switch working directory (the robot scripts read local files)."""
    old_dir = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old_dir)


# ---------------------------------------------------------------------------
# Actually run a command on the arm
# ---------------------------------------------------------------------------

def execute_command(data):
    """
    Run one command on the arm and return a dict of fields to save back.

    `data` is the Firestore document contents. The return value is merged into
    the document so the app can see the result.
    """
    command = data.get("command", "")
    item = data.get("item", "")
    day = data.get("day", "")

    # ---- Mock mode: pretend to work so you can test without the arm ----
    if MOCK_MODE:
        time.sleep(2)
        if command == "check_shelf":
            return {"emptySlots": ["Monday", "Wednesday"]}
        return {}

    # ---- Real mode: call the same functions the HTTP backend used ----
    with change_dir(SCRIPTS_PATH):
        if command == "pickup":
            position_code = get_position(item)
            pick_up(position_code)
            return {}

        if command == "shelve":
            shelf_meds(day)
            return {}

        if command == "check_shelf":
            empty_slots = check_shelf()
            return {"emptySlots": list(empty_slots)}

    raise ValueError(f"Unknown command: {command!r}")


# ---------------------------------------------------------------------------
# Claim + process commands
# ---------------------------------------------------------------------------

# Only ONE command may run at a time (the arm is a single physical resource).
work_queue: "queue.Queue" = queue.Queue()


def claimable_status():
    """Which status this listener is allowed to pick up."""
    return "queued" if REQUIRE_VALIDATION else "pending"


def try_claim(db, doc_id):
    """
    Atomically move a command from its claimable status to "in_progress".

    Using a transaction guarantees that if two machines (or a restarted script)
    see the same command, only ONE of them wins the claim. Returns the document
    data if we won the claim, otherwise None.
    """
    doc_ref = db.collection(COLLECTION_NAME).document(doc_id)
    transaction = db.transaction()

    @firestore.transactional
    def _claim(txn):
        snapshot = doc_ref.get(transaction=txn)
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        if data.get("status") != claimable_status():
            return None
        txn.update(
            doc_ref,
            {
                "status": "in_progress",
                "startedAt": firestore.SERVER_TIMESTAMP,
            },
        )
        return data

    return _claim(transaction)


def worker_loop(db):
    """Runs in its own thread and processes claimed commands one by one."""
    while True:
        doc_id, data = work_queue.get()
        doc_ref = db.collection(COLLECTION_NAME).document(doc_id)
        logger.info("Executing command %s (%s)", doc_id, data.get("command"))
        try:
            result = execute_command(data)
            update = {
                "status": "success",
                "errorMessage": "",
                "finishedAt": firestore.SERVER_TIMESTAMP,
            }
            update.update(result)
            doc_ref.update(update)
            logger.info("Command %s finished: success", doc_id)
        except Exception as exc:  # noqa: BLE001 - report every failure to the app
            logger.exception("Command %s failed", doc_id)
            doc_ref.update(
                {
                    "status": "failed",
                    "errorMessage": str(exc),
                    "finishedAt": firestore.SERVER_TIMESTAMP,
                }
            )
        finally:
            work_queue.task_done()


def on_snapshot(db):
    """Returns the callback Firestore calls whenever the query result changes."""

    def _callback(col_snapshot, changes, read_time):
        for change in changes:
            # We only care about brand new / newly-eligible documents.
            if change.type.name not in ("ADDED", "MODIFIED"):
                continue
            doc_id = change.document.id
            data = change.document.to_dict() or {}
            if data.get("status") != claimable_status():
                continue
            claimed = try_claim(db, doc_id)
            if claimed is not None:
                logger.info("Claimed command %s", doc_id)
                work_queue.put((doc_id, claimed))

    return _callback


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # 1) Authenticate to Firebase using the service account key.
    if os.path.exists(SERVICE_ACCOUNT_PATH):
        cred = credentials.Certificate(SERVICE_ACCOUNT_PATH)
        firebase_admin.initialize_app(cred)
    else:
        # Falls back to GOOGLE_APPLICATION_CREDENTIALS env var if the file is
        # not next to this script.
        logger.warning(
            "serviceAccountKey.json not found at %s - trying default credentials.",
            SERVICE_ACCOUNT_PATH,
        )
        firebase_admin.initialize_app()

    db = firestore.client()

    logger.info("Connected to Firebase.")
    logger.info("Mock mode: %s | Requires validation: %s", MOCK_MODE, REQUIRE_VALIDATION)
    logger.info("Listening for '%s' commands in '%s'...", claimable_status(), COLLECTION_NAME)

    # 2) Start the single worker thread that runs commands on the arm.
    threading.Thread(target=worker_loop, args=(db,), daemon=True).start()

    # 3) Subscribe to only the commands we can act on. This is a live query:
    #    Firestore pushes changes to us; we never poll.
    query = db.collection(COLLECTION_NAME).where(
        filter=firestore.FieldFilter("status", "==", claimable_status())
    )
    query.on_snapshot(on_snapshot(db))

    # 4) Keep the process alive so the listener stays connected.
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down listener.")


if __name__ == "__main__":
    main()
