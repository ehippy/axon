"""Hermes CLI + gateway plugin: Axon activity and bounded per-session reporting context."""
from pathlib import Path
import importlib.util
import json
import subprocess
import sys
import threading
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
from _axon_hermes_runtime.plugin_runtime import claim, context, delivery, inbox_due, lifecycle, report, wake_enabled


def safely(callback, *args, **kwargs):
    try:
        return callback(*args, **kwargs)
    except Exception:
        return None


def before_turn(session_id=None, parent_session_id=None, cwd=None, **_):
    if not session_id:
        return None
    safely(lifecycle,'hermes',{'session_id':session_id,'parent_session_id':parent_session_id,
                              'hook_event_name':'UserPromptSubmit','cwd':cwd})
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


def after_tool(session_id=None, tool_name='', cwd=None, **_):
    if session_id:
        safely(lifecycle,'hermes',{'session_id':session_id,'tool_name':tool_name,'hook_event_name':'PostToolUse','cwd':cwd})


def session_start(session_id=None, cwd=None, **_):
    if session_id:
        safely(lifecycle,'hermes',{'session_id':session_id,'hook_event_name':'SessionStart','cwd':cwd})


def turn_end(session_id=None, interrupted=False, failed=False, cwd=None, **_):
    if not session_id:
        return
    if failed:
        safely(report.report,'hermes',session_id,'blocked',{'blocker':'Agent turn failed; inspect Hermes logs'})
    else:
        safely(lifecycle,'hermes',{'session_id':session_id,'hook_event_name':'Interrupt' if interrupted else 'Stop','cwd':cwd})


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


class NativeWake:
    """Native injection between turns; existing safe-point hooks handle busy input.

    Hermes' classic CLI inject_message interrupts busy work. The lifecycle lock
    prevents that API from running while a tool call can be in progress.
    """
    def __init__(self,ctx):
        self.ctx=ctx
        self.lock=threading.RLock()
        self.sessions={}
        self.closed=False

    def capture(self,session,kwargs):
        entry=self.sessions.setdefault(session,{'busy':True,'process':None,'route':'','surface':'','cwd':''})
        surface=safely(getattr(self.ctx,'session_surface',lambda:'')) or kwargs.get('platform') or ''
        cwd=safely(getattr(self.ctx,'session_cwd',lambda:'')) or ''
        if surface:entry['surface']=surface
        if cwd:entry['cwd']=cwd
        route=kwargs.get('session_key')
        if not route:
            try:
                from hermes_cli.plugin_session import session_key
                route=session_key()
            except ImportError:
                pass
        if route:entry['route']=route
        if self.classic():entry['route']=session
        return entry

    def classic(self):
        return getattr(getattr(self.ctx,'_manager',None),'_cli_ref',None) is not None

    def stop(self,entry):
        child=entry['process'];entry['process']=None
        if child:
            try:
                child.terminate();child.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                child.kill();child.wait(timeout=.5)
            except OSError:
                pass

    def unavailable(self,session,reason):
        safely(report.report,'hermes',session,'heartbeat',{'activity':'Wake unavailable: '+reason})

    def permitted(self,session,entry):
        if not entry['route']:
            self.unavailable(session,'Hermes session route is missing')
            return False
        if not self.classic() and safely(getattr(self.ctx,'_gateway_injection_allowed',lambda:False)) is not True:
            self.unavailable(session,'enable Hermes allow_gateway_injection')
            return False
        return True

    def start(self,session,entry):
        if self.closed or entry['process'] or not wake_enabled('hermes') or not self.permitted(session,entry):return
        try:
            child=subprocess.Popen([sys.executable,str(runtime/'native_inbox.py'),'--agent','hermes'],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
            entry['process']=child
            child.stdin.write(json.dumps({'session_id':session})+'\n');child.stdin.flush()
            threading.Thread(target=self.receive,args=(session,entry,child),daemon=True).start()
        except (OSError,ValueError):
            self.stop(entry)

    def receive(self,session,entry,child):
        try:
            for line in child.stdout:
                batch=json.loads(line).get('messages',[])
                if not batch:continue
                with self.lock:
                    accepted=False
                    if not self.closed and not entry['busy'] and entry['process'] is child and self.permitted(session,entry):
                        accepted=safely(self.ctx.inject_message,delivery(batch),session_key=entry['route']) is True
                        if not accepted:self.unavailable(session,'Hermes did not accept native input')
                    child.stdin.write(json.dumps({'accepted':accepted})+'\n');child.stdin.flush()
        except (OSError,ValueError):
            pass
        finally:
            with self.lock:
                if entry['process'] is child:self.stop(entry)
            for stream in (child.stdin,child.stdout):
                if stream:safely(stream.close)
            safely(child.wait,timeout=.5)

    def wrap(self,callback,phase=None):
        def run(**kwargs):
            session=kwargs.get('session_id')
            if not session:return callback(**kwargs)
            with self.lock:
                entry=self.capture(session,kwargs)
                if phase=='busy' and self.classic():
                    for old in list(self.sessions):
                        if old!=session:self.stop(self.sessions.pop(old))
                if phase in ('busy','closed'):
                    entry['busy']=True
                if phase=='closed':self.stop(entry)
                enriched={**kwargs,'cwd':entry['cwd'] or None}
            result=callback(**enriched)
            with self.lock:
                if phase=='idle':
                    entry['busy']=False;self.start(session,entry)
                elif phase=='closed':self.sessions.pop(session,None)
            return result
        return run

    def reset(self,session_id=None,session_key=None,**_):
        with self.lock:
            classic=self.classic()
            for session,entry in list(self.sessions.items()):
                if classic or session==session_id or (session_key and entry['route']==session_key):
                    self.stop(entry);self.sessions.pop(session,None)

    def close(self):
        with self.lock:
            self.closed=True
            for entry in self.sessions.values():self.stop(entry)
            self.sessions.clear()


def register(ctx):
    wake=NativeWake(ctx)
    phases={'pre_llm_call':'busy','on_session_start':'busy','on_session_end':'idle','on_session_finalize':'closed'}
    for name, callback in {'pre_llm_call':before_turn,'post_tool_call':after_tool,
            'transform_tool_result':tool_result,'pre_verify':verify,
            'on_session_start':session_start,'on_session_end':turn_end,
            'on_session_finalize':session_finalize,'subagent_start':child_start,
            'subagent_stop':child_stop}.items():
        ctx.register_hook(name,wake.wrap(callback,phases.get(name)))
    if hasattr(ctx,'on_unload'):ctx.on_unload(wake.close)
    ctx.register_hook('on_session_reset',wake.reset)
