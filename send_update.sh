#!/bin/bash
cd /Users/ph/church-rides
export GOOGLE_APPLICATION_CREDENTIALS=/Users/ph/church-rides-key.json
source venv/bin/activate
python -c "
from dotenv import load_dotenv
load_dotenv()
from functions.read_riders_sheet import get_next_sunday_date
from functions.send_weekly_emails import send_saturday_update
sunday = get_next_sunday_date()
result = send_saturday_update(sunday)
print(result)
" >> /Users/ph/church-rides/cron.log 2>&1
