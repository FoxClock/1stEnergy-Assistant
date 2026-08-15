"""
Author:     Hayden Foxwell
Date:       15/08/2026
Purpose:
    To outline the API for connecting to 1st energy to download usage statistics.
"""

## Imports ##
from src.custom_components.first_energy.const import *

#############
## Globals ##

#############

## Classes ##
class FirstEnergyApi:
    async def login(user: User) -> Account:
        ...
    async def get_balance(account: Account) -> Invoice:
        ...
    async def get_daily(account: Account, interval= TIME_INTERVALS["DEFAULT"]):
        ...
    async def get_halfhour(...)


