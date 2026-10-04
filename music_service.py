# -*- coding: utf-8 -*-
"""Entry point of the playback service (see buildozer.spec: services = Music:music_service.py).
All the logic lives in engine.py."""

import os

if not os.environ.get('KAZE_TEST'):         # tests import this file without starting the loop
    from engine import run_service
    run_service()
