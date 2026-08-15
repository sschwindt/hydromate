"""A stand-in for TELEMAC's own Python package.

``TelemacRuntime.check_available`` proves an environment by importing TELEMAC's SELAFIN
reader inside it. Without this, a fake environment can be *captured* but never counts as
*available*, and the happy path of a submitted job could not be tested at all.
"""
