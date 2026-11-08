"""Unattended data collectors.

Each subpackage is one collection lane with its own archive prefix, schedule
and deployment entry point. Nothing here is imported by the services that read
the archives; a lane only writes.
"""
