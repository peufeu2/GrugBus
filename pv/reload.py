#!/usr/bin/python
# -*- coding: utf-8 -*-

import os, time, sys, logging, asyncio, importlib

from path import Path
import config

log = logging.getLogger(__name__)

########################################################################################
#
#       Reload code when changed
#
########################################################################################

# List of modules to reload (this can be updated to add some at runtime)
# Note CPython dicts are ordered, so they will be loaded in dict order.
modules_to_reload = {}

def add_module_to_reload( module_name, callback=None ):
    modules_to_reload[module_name] = callback

# modules should contain names, not modules
async def reload_coroutine( ):
    old_mtime = None

    while True:
        try:
            # look up modules to reload so we get latest versions
            await asyncio.sleep(0.5)

            # check mtime on the CURRENT FILE so we don't reload modules out of order
            # while they are being copied
            mtime = Path( __file__ ).mtime
            if not old_mtime:
                old_mtime = mtime

            elif old_mtime != mtime:
                old_mtime = mtime
                for module, callback in list(modules_to_reload.items()):
                    log.info( "Reloading: %s", module )
                    
                    # unload callback
                    if func := getattr( module, "on_module_unload", None):
                        func()

                    # reload module
                    importlib.reload( sys.modules[module] )

                    # after reload callback
                    if callback:
                        callback()

        except Exception:
            log.exception("Reload coroutine:")



########################################################################################
#
#       Wrapper for reloadable code
#
########################################################################################
#   To reload the module, getfunc must be a function
#   that looks up the module when it is restarted,
#   not when this object is initialized
#
async def reloadable_coroutine( title, getfunc, *args, **kwargs ):
    text = "Start: "
    first_start = True
    try:
        while True:
            try:
                log.info(text+title )
                func = getfunc()                # look up func in reloadable module
                def module_updated():
                    return func != getfunc()      # look it up again to see if it was reloaded
                await func( module_updated, first_start, *args, **kwargs )

            except Exception:
                log.exception("")
                await asyncio.sleep(1)

            text = "Restart: "
            first_start = False
    finally:
        log.info("Exit: "+title )
