"""Hermes CLI + gateway plugin: Axon activity and bounded per-session reporting context."""
from pathlib import Path
import importlib.util
import sys
root = Path(__file__).resolve().parent
runtime_root = root / 'runtime'
if runtime_root.is_dir():
    sys.path.insert(0, str(runtime_root))
    runtime = runtime_root / 'axon_reporter'
else:
    projects = root.parents[1] / 'packages/python'
    sys.path.insert(0, str(projects / 'axon_protocol'))
    runtime = projects / 'axon_reporter/axon_reporter'
# Hermes runs plugins in-process. Keep generic report/journal module names private.
name = '_axon_hermes_runtime'
if name not in sys.modules:
    spec = importlib.util.spec_from_file_location(name,runtime/'__init__.py',submodule_search_locations=[str(runtime)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
from _axon_hermes_runtime.plugin_runtime import claim, context, delivery, inbox_due, lifecycle, report


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
    notes = [context('hermes',session_id)]
    # Drain dashboard input queued since the last turn alongside the fresh prompt.
    messages = safely(claim,'hermes',session_id,timeout=.5) or []
    if messages:
        notes.append(delivery(messages))
    return {'context':'\n\n'.join(notes)}


def tool_result(result=None, session_id=None, **_):
    """Mid-turn dashboard input: piggyback the next tool result the model reads (throttled)."""
    if not session_id or not isinstance(result,str) or not safely(inbox_due,'hermes',session_id):
        return None
    messages = safely(claim,'hermes',session_id,timeout=.5) or []
    return result + '\n\n' + delivery(messages) if messages else None


def verify(session_id=None, **_):
    """Input that arrives just as a turn finishes deserves an answer: keep the turn alive once
    (core only fires this hook while nudge budget remains, so a claimed message is never dropped)."""
    if not session_id:
        return None
    messages = safely(claim,'hermes',session_id,timeout=.5) or []
    return {'action':'continue','message':delivery(messages)} if messages else None


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
        def emit():
            source, hashed, _ = report.identity('hermes',str(parent_session_id)) if parent_session_id else ('','','')
            report.report('hermes',child_session_id,'start',{'parent':f'{source}/{hashed}' if parent_session_id else ''})
        safely(emit)


def child_stop(child_session_id=None,child_status=None,**_):
    if child_session_id:
        # A child exit isn't evidence that its task was completed.
        turn_end(session_id=child_session_id,interrupted=child_status=='interrupted',failed=child_status in ('failed','error'))


def register(ctx):
    for name, callback in {'pre_llm_call':before_turn,'post_tool_call':after_tool,
            'transform_tool_result':tool_result,'pre_verify':verify,
            'on_session_start':session_start,'on_session_end':turn_end,
            'on_session_finalize':session_finalize,'subagent_start':child_start,
            'subagent_stop':child_stop}.items():
        ctx.register_hook(name,callback)
