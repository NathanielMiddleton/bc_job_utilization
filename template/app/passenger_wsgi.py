"""Entry point for Phusion Passenger when installed as an OnDemand system app
(copy this directory to /var/www/ood/apps/sys/job-utilization)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server import application  # noqa: E402,F401
