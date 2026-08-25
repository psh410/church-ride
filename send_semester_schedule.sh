#!/bin/bash
cd /Users/ph/church-rides
export GOOGLE_APPLICATION_CREDENTIALS=/Users/ph/church-rides-key.json
source venv/bin/activate
python -c "
from dotenv import load_dotenv
load_dotenv()
from functions.send_semester_schedule import send_monday_schedule
result = send_monday_schedule()
print(result)
" >> /Users/ph/church-rides/cron.log 2>&1
