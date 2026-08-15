"""
Author:     Hayden Foxwell
Date:       15/08/2026
Purpose:
    Define custom exceptions for the API to use.
"""

## Imports ##

#############
## Globals ##

#############

## Base Exception Exception ##
class ApiError(Exception):

    def __init__(self, status, body: str) -> None:
        super().__init__(f"HTTP {status} {body[:300]}")
        self.status, self.body = status, body