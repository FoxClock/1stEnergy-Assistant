"""
Author:     Hayden Foxwell
Date:       15/08/2026
Purpose:
    To outline the Constants for HACs integrations.
"""

## Imports ##
from typing import Any
from zoneinfo import ZoneInfo

#############
## Globals ##

#############

## Constants ##
TIME_INTERVALS: dict[str, Any]  = {
    "DEFAULT": None,
    "NONE": "NONE",
    "HALF_HR": "MIN_30",
    "FULL": "FULL"
}

API = "https://endpoint-firstenergy-mobileapp-prod-dchufubea3frdfhc.a01.azurefd.net"
PORTAL = "https://myaccount.1stenergy.com.au"
TZ = ZoneInfo("Australia/Sydney")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")