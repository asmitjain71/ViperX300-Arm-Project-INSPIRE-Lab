$env:PYTHONPATH = "C:\Users\Administrator\Downloads\ViperX300-Arm-Project-INSPIRE-Lab\Sem1 2024-25 - VLN Based Medicine Management System"
Write-Host "Starting ViperX300 Backend..."
cd backend
python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
