"""Workflow registration.

Importing this package registers every use case this build implements.
Registration is explicit and happens once, at import, so that an unknown
`use_case_id` is rejected at the API boundary rather than accepted as a job
that is guaranteed to fail when a worker eventually picks it up.
"""

from app.usecases import probe
from app.usecases.registry import register
from app.usecases.scholarship_finder import graph as scholarship_finder

register(probe.USE_CASE_ID, probe.build)
register(scholarship_finder.USE_CASE_ID, scholarship_finder.build)
