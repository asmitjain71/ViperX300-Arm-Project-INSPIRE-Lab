
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import sys
import os
import threading
import logging
import contextlib
import subprocess

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Add the directory containing the robot scripts to sys.path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
scripts_path = os.path.join(project_root, "Sem1 2024-25 - VLN Based Medicine Management System")
sys.path.append(scripts_path)

# Try importing the robot modules.
try:
    from check_shelf import check_shelf
    from shelve_meds import shelf_meds
    MOCK_MODE = False
except ImportError as e:
    logger.error(f"Failed to import robot modules: {e}")
    logger.warning("Running in MOCK MODE for testing purposes.")
    MOCK_MODE = True

app = FastAPI(title="ViperX300 Arm Controller API")

robot_lock = threading.Lock()

class ShelveRequest(BaseModel):
    day: str

class PickupRequest(BaseModel):
    item_name: str

# Context manager to temporarily change the working directory
@contextlib.contextmanager
def change_dir(path):
    old_dir = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old_dir)

@app.get("/")
def read_root():
    return {"status": "online", "mock_mode": MOCK_MODE}

@app.get("/inventory/check")
def check_inventory():
    if robot_lock.locked():
        raise HTTPException(status_code=503, detail="Robot is busy")
    
    with robot_lock:
        try:
            if MOCK_MODE:
                import time
                time.sleep(2)
                return {"empty_slots": ["Monday", "Wednesday"]}
            
            with change_dir(scripts_path):
                empty_slots = check_shelf()
                return {"empty_slots": empty_slots}
        except Exception as e:
            logger.error(f"Error checking shelf: {e}")
            raise HTTPException(status_code=500, detail=str(e))

@app.post("/control/shelve")
def shelve_medicine(request: ShelveRequest):
    if robot_lock.locked():
        raise HTTPException(status_code=503, detail="Robot is busy")

    with robot_lock:
        try:
            if MOCK_MODE:
                return {"status": "success", "message": f"Shelved to {request.day} (Mock)"}
            
            with change_dir(scripts_path):
                shelf_meds(request.day)
                return {"status": "success", "message": f"Shelved to {request.day}"}
        except Exception as e:
            logger.error(f"Error shelving medicine: {e}")
            raise HTTPException(status_code=500, detail=str(e))

@app.post("/control/pickup")
def pickup_medicine(request: PickupRequest):
    if robot_lock.locked():
        raise HTTPException(status_code=503, detail="Robot is busy")

    with robot_lock:
        try:
            if MOCK_MODE:
                return {"status": "success", "message": f"Picked up {request.item_name} (Mock)"}
            
            with change_dir(scripts_path):
                python = "python3" if os.name != "nt" else sys.executable
                result = subprocess.run(
                    [python, "test.py"], cwd=scripts_path, check=True
                )
                if result.returncode != 0:
                    raise RuntimeError(
                        f"test.py exited with code {result.returncode}"
                    )
            
            return {"status": "success", "message": f"Picked up {request.item_name}"}
        except Exception as e:
            logger.error(f"Error picking up medicine: {e}")
            raise HTTPException(status_code=500, detail=str(e))
