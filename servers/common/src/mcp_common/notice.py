"""The fictional-data notice every CRM and handbook result carries.

Harborline Supply Co. does not exist. A result that is shown to a person, or pasted into a
screenshot, should say so without anyone having to remember to add it.
"""

from pydantic import BaseModel

FICTIONAL_NOTICE = "Harborline Supply Co. is fictional; all of this data is synthetic."


class FictionalOutput(BaseModel):
    """Base of a tool's output: the notice comes first, so a client sees it before the data."""

    notice: str = FICTIONAL_NOTICE
