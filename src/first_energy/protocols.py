"""
Author:     Hayden Foxwell
Date:       15/08/2026
Purpose:
    Create protocols for use in the first energy system.
"""

## Imports ##
from typing import Protocol

#############
## Globals ##

#############

## Protocols ##
class EnergyClient(Protocol):

    async def login(self) -> None:
        ...

    async def account(self) -> Account:
        ...

    async def usage(self) -> list[UsageRecord]:
        ...
    