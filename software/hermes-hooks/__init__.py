"""Hermes CLI + gateway plugin: Axon activity and bounded per-session reporting context."""
from pathlib import Path
import importlib.util
import sys
root = Path(__file__).resolve().parent
runtime = root/'runtime' if (root/'runtime').is_dir() else root.parent/'activity-journal'
# Hermes runs plugins in-process. Keep generic report/journal module names private.
name = '_axon_hermes_runtime'
if name not in sys.modules:
    spec = importlib.util.spec_from_file_location(name,runtime/'__init__.py',submodule_search_locations=[str(runtime)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
from _axon_hermes_runtime.plugin_runtime import context, lifecycle, report


def safely(callback, *args, **kwargs):
    try:
        return callback(*args, **kwargs)
    except Exception:
        return None


def before_turn(session_id=None, parent_session_id=None, **_):
    if not session_id:
        return None
    safely(lifecycle,'hermes',{'session_id':session_id,'parent_session_id':parent_session_id,
                              'hook_event_name':'UserPromptSubmit'})
    return {'context':context('hermes',session_id)}


def after_tool(session_id=None, tool_name='', **_):
    if session_id:
        safely(lifecycle,'hermes',{'session_id':session_id,'tool_name':tool_name,'hook_event_name':'PostToolUse'})


def session_start(session_id=None, **_):
    if session_id:
        safely(lifecycle,'hermes',{'session_id':session_id,'hook_event_name':'SessionStart'})


def turn_end(session_id=None, interrupted=False, failed=False, **_):
    if not session_id:
        return
    if failed:
        safely(report.report,'hermes',session_id,'blocked',{'blocker':'Agent turn failed; inspect Hermes logs'})
    else:
        safely(lifecycle,'hermes',{'session_id':session_id,'hook_event_name':'Interrupt' if interrupted else 'Stop'})


def session_finalize(session_id=None, **_):
    if session_id:
        safely(lifecycle,'hermes',{'session_id':session_id,'hook_event_name':'SessionEnd'})


def child_start(child_session_id=None,parent_session_id=None,**_):
    if child_session_id:
        source, hashed, _ = report.identity('hermes',str(parent_session_id)) if parent_session_id else ('','','')
        safely(report.report,'hermes',child_session_id,'start',{'parent':f'{source}/{hashed}' if parent_session_id else ''})


def child_stop(child_session_id=None,child_status=None,**_):
    if child_session_id:
        # A child exit isn't evidence that its task was completed.
        turn_end(session_id=child_session_id,interrupted=child_status=='interrupted',failed=child_status in ('failed','error'))


def register(ctx):
    for name, callback in {'pre_llm_call':before_turn,'post_tool_call':after_tool,
            'on_session_start':session_start,'on_session_end':turn_end,
            'on_session_finalize':session_finalize,'subagent_start':child_start,
            'subagent_stop':child_stop}.items():
        ctx.register_hook(name,callback)
