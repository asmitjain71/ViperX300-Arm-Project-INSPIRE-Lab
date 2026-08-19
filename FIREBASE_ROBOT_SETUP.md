# Connecting the App to the Robot Arm through Firebase (Option B)

This guide explains, from the very basics, how the Flutter app tells the
ViperX300 arm what to do **without ever connecting to it directly**. It is
written for a beginner. Read it top to bottom once, then follow the checklist.

---

## 1. The big idea (in plain English)

Normally you would make the phone app talk straight to the robot PC over the
network (that is "Option A", the HTTP backend in `backend/main.py`). The problem:
the phone and the robot PC must be on the same network, and you often have to
open a network port on the lab machine, which IT departments do not like.

**Option B removes that problem using Firebase as a "shared notebook" in the
cloud:**

```
   ┌──────────────┐        writes a         ┌───────────────────────┐
   │  Flutter app │ ─────  command  ───────▶ │  Firebase (Firestore) │
   │  (caregiver) │                          │   RobotCommands/<id>  │
   │              │ ◀──── reads status ───── │                       │
   └──────────────┘      in real time        └───────────┬───────────┘
                                                          │  listens & updates
                                                          ▼
                                              ┌───────────────────────┐
                                              │   Robot PC (Python)   │
                                              │  firebase_listener.py │
                                              │   → moves the arm     │
                                              └───────────────────────┘
```

- The **app** only writes to Firebase and reads from Firebase.
- The **robot PC** only reads from Firebase and writes back to Firebase.
- **Neither one connects directly to the other.** Both only make *outbound*
  connections to Google's servers, so **no port needs to be opened** on the lab
  network. This works even if the robot is in a different building or city.

Think of `RobotCommands` as a shared to-do list. The caregiver adds a sticky
note ("pick up Liv52"). The robot PC is always watching the list, grabs the
note, does the job, and writes "Done ✅" on the same note. The app sees the note
change instantly.

---

## 2. The command "document" — the heart of the system

Every action is one document in the Firestore collection `RobotCommands`. It
looks like this:

```jsonc
// RobotCommands/aB12xY...   (the document id is auto-generated)
{
  "command": "pickup",          // "pickup" | "shelve" | "check_shelf"
  "item": "Liv52 Himalaya",     // which medicine (for pickup)
  "day": "",                    // which slot (for shelve, e.g. "Monday")
  "requestedBy": "<caregiver uid>",
  "status": "pending",          // the lifecycle field everyone watches
  "emptySlots": [],             // filled in after a check_shelf
  "errorMessage": "",           // filled in if something fails
  "createdAt": <server time>,
  "finishedAt": null
}
```

### The lifecycle of `status`

```
pending ──▶ (queued) ──▶ in_progress ──▶ success
   │                                  └─▶ failed
   └────────────────────────────────────▶ rejected
```

- **pending** – the app just created it.
- **queued** – *only if you use the optional Cloud Function*; means "validated".
- **in_progress** – the robot PC claimed it and the arm is moving.
- **success / failed** – the arm finished (or hit an error).
- **rejected** – the Cloud Function refused it (wrong role, rate limit, no stock).

Everyone updates this one field and watches everyone else's updates. That is the
entire protocol.

---

## 3. What was added to your project

| File | Where | What it does |
|------|-------|--------------|
| `lib/model/robot_command.dart` | App | Dart class describing one command document. |
| `lib/services/robot_command_service.dart` | App | Writes commands & streams live status. |
| `lib/inventory_management_page.dart` | App | Buttons now use Firebase + a live status dialog. |
| `robot_pc/firebase_listener.py` | Robot PC | Watches Firestore and drives the arm. |
| `robot_pc/requirements.txt` | Robot PC | The one new Python dependency. |
| `functions/main.py` | Cloud (optional) | Validates role, rate-limits, reserves stock. |
| `firestore.rules` | Firebase | Security rules for `RobotCommands`. |
| `firebase.json` | Firebase | Registers the Cloud Functions folder. |

The old HTTP `ArmService` (`lib/services/arm_service.dart`) and `backend/main.py`
are left untouched so you can still use Option A if you ever want to.

---

## 4. Setup — do this once

### Step 4.1 — App side (Flutter)

Good news: the app already uses Firebase (project `medicapp-f2c65`) and already
depends on `cloud_firestore`. There is **nothing new to install**. Just build and
run the app as usual:

```powershell
cd FlutterDoctorApp-main\FlutterDoctorApp-main
flutter pub get
flutter run
```

### Step 4.2 — Publish the security rules

The new rules in `firestore.rules` let a signed-in caregiver create a command and
read it back. Publish them:

```powershell
cd FlutterDoctorApp-main\FlutterDoctorApp-main
firebase deploy --only firestore:rules
```

(If you do not have the Firebase CLI: `npm install -g firebase-tools`, then
`firebase login`.)

### Step 4.3 — Robot PC side (Python listener)

1. **Get a service account key** (this is how the robot PC proves it is allowed
   to use your Firebase project):
   - Firebase Console → your project → ⚙ **Project settings** → **Service accounts**
   - Click **Generate new private key** → a `.json` file downloads.
   - Rename it to `serviceAccountKey.json` and put it inside the `robot_pc/`
     folder, right next to `firebase_listener.py`.
   - ⚠️ **Never share or commit this file.** It is a master password to your
     project. (`robot_pc/.gitignore` already blocks it from Git.)

2. **Install the one dependency** on the robot PC:

   ```bash
   cd robot_pc
   pip install -r requirements.txt
   ```

3. **Test without the arm first (recommended).** Open `firebase_listener.py` and
   set `FORCE_MOCK_MODE = True`. This makes the listener pretend to move the arm
   so you can confirm the Firebase plumbing works. Then:

   ```bash
   python firebase_listener.py
   ```

   You should see `Connected to Firebase.` and `Listening for 'pending'
   commands...`. Now tap **Pick Up** in the app — the console should log
   `Claimed command ...` → `Executing ...` → `success`, and the app dialog should
   move Pending → In Progress → Completed.

4. **Go live.** Once the mock test works, set `FORCE_MOCK_MODE = False` and run it
   again on the actual robot PC (the one with the arm and RealSense camera
   plugged in). It will now call the real `pick_up`, `shelf_meds`, and
   `check_shelf` functions from `Sem1 2024-25 - VLN Based Medicine Management
   System`.

That's it — the core system is working. Steps 4.1–4.3 are all you need.

---

## 5. Optional — the Cloud Function (extra safety)

The Cloud Function in `functions/main.py` adds a trusted gatekeeper that runs in
Google's cloud **before** the arm moves. It:

- checks the requester's `role` (only `caregiver`/`admin`/`doctor` allowed),
- rate-limits (max 5 commands/minute per user),
- for a pickup, atomically reserves one unit from inventory (so you can't
  over-dispense).

If it approves, it changes `status` from `pending` to `queued`. If not, it sets
`status` to `rejected` with a reason the caregiver sees in the app.

**Deploying it requires the Firebase "Blaze" (pay-as-you-go) plan** — Cloud
Functions are not available on the free plan. If you are only demoing the
project, you can skip this section entirely.

To deploy:

1. Open `functions/main.py` and confirm `DATABASE_URL` matches your Realtime
   Database URL (Firebase Console → Realtime Database).
2. Deploy:

   ```powershell
   cd FlutterDoctorApp-main\FlutterDoctorApp-main
   firebase deploy --only functions
   ```

3. **Important:** because the function now guards every command, tell the robot
   PC to only run *validated* commands. In `robot_pc/firebase_listener.py` set:

   ```python
   REQUIRE_VALIDATION = True
   ```

   Now the flow is: app → `pending` → (Cloud Function validates) → `queued` →
   (robot runs it) → `success`.

> Note on inventory: the function reserves stock *before* the arm moves, matching
> the design. If the arm then fails, that reserved unit is not auto-restored —
> an admin can reconcile the count from the app's inventory screen.

---

## 6. How the pieces line up with your existing code

- The listener imports the **same** functions the HTTP backend used:
  `pick_up`, `get_position`, `shelf_meds`, `check_shelf`. So the arm behaves
  identically to Option A — only the *transport* changed (Firebase instead of
  HTTP).
- Inventory still lives in the **Realtime Database** under `/inventory` exactly
  as `inventory_management_page.dart` expects. The Cloud Function decrements it
  there.
- Commands live in **Firestore** under `/RobotCommands`. Firestore is used here
  because its document + real-time-listener model is a perfect fit for a command
  with a changing `status`.

---

## 7. Quick troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| App: "You must be signed in to command the robot." | Log in first; commands need `request.auth.uid`. |
| App dialog stuck on **Pending** forever | The listener isn't running, or `REQUIRE_VALIDATION` is `True` but no Cloud Function is deployed. |
| Listener: `serviceAccountKey.json not found` | Put the key in `robot_pc/` or set `GOOGLE_APPLICATION_CREDENTIALS`. |
| Listener: `Could not import robot modules` | Run it on the robot PC (with interbotix/pyrealsense installed), or set `FORCE_MOCK_MODE = True` to test. |
| Command becomes **rejected** immediately | The Cloud Function blocked it — check the caregiver's `role` in `users/<uid>` and the rate limit. |
| `firebase deploy --only functions` fails | Functions need the Blaze plan; upgrade or skip the optional function. |
```
