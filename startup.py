# -*- coding: utf-8 -*-
"""pyDQT-Design startup: records that Revit loaded this extension - see
lib/dqt_usage.py. It never stops pyRevit from loading."""
try:
    from dqt_usage import log_session
    log_session(__file__)
except Exception:
    pass
