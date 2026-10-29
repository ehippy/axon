"""Project inventory and user overrides without rewriting reported history."""
import json
import math
import time


def project_key(session):
    return session.get('project_id') or ('legacy:'+session['project'] if session.get('project') else None)


def canonical(key, overrides):
    seen=set()
    while overrides.get(key, {}).get('merge_into'):
        if key in seen:
            raise ValueError('project merge cycle')
        seen.add(key)
        key=overrides[key]['merge_into']
    return key


class ProjectStore:
    def init_projects(self):
        with self.connect() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS project_names (
                project_id TEXT PRIMARY KEY, name TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS project_checkouts (
                project_id TEXT, machine TEXT, worktree TEXT, name TEXT, branch TEXT,
                seen_at REAL, PRIMARY KEY(project_id,machine,worktree));
              CREATE TABLE IF NOT EXISTS project_overrides (
                project_id TEXT PRIMARY KEY, name TEXT, hidden INTEGER DEFAULT 0, merge_into TEXT);
            ''')
            # Upgrade projections created before this inventory existed.
            for row in db.execute('SELECT payload,at FROM sessions').fetchall():
                self.project_seen(db,json.loads(row['payload']),row['at'])

    def project_seen(self, db, session, at):
        if session.get('project_id') and session.get('machine') and session.get('project'):
            db.execute('INSERT OR IGNORE INTO project_names VALUES(?,?)', (session['project_id'],session['project']))
            db.execute('''INSERT INTO project_checkouts VALUES(?,?,?,?,?,?)
              ON CONFLICT(project_id,machine,worktree) DO UPDATE SET
              name=excluded.name,branch=excluded.branch,seen_at=excluded.seen_at
              WHERE excluded.seen_at>=project_checkouts.seen_at''',
                (session['project_id'],session['machine'],session.get('worktree',''),
                 session['project'],session.get('branch',''),at))

    def project_overrides(self):
        with self.connect() as db:
            return {r['project_id']:dict(r) for r in db.execute('SELECT * FROM project_overrides')}

    def project_names(self):
        with self.connect() as db:
            return {r['project_id']:r['name'] for r in db.execute('SELECT * FROM project_names')}

    def apply_project_override(self, session, overrides, names):
        key=project_key(session)
        if not key:
            return session
        if key.startswith('legacy:') and key not in overrides:
            # A label from before git identities names a repository only when exactly one
            # known repository has that name; any other label was a topic, not a project.
            ids=[i for i,name in names.items() if name==session['project']]
            if len(ids)!=1:
                session={k:v for k,v in session.items() if k!='project'}
                session['topic']=session.get('topic') or key.removeprefix('legacy:')
                return session
            key=ids[0]
        target=canonical(key,overrides)
        override=overrides.get(target,{})
        session={**session,'project_key':target,'project_hidden':bool(override.get('hidden'))}
        if not target.startswith('legacy:'):
            session['project_id']=target
        if override.get('name'):
            session['project']=override['name']
        elif names.get(target):
            session['project']=names[target]
        return session

    def projects(self, since=None):
        since=time.time()-86400 if since is None else since
        if not isinstance(since,(int,float)) or not math.isfinite(since) or since<0:
            raise ValueError('invalid period')
        overrides=self.project_overrides()
        groups={}
        with self.connect() as db:
            checkouts=[dict(r) for r in db.execute('SELECT * FROM project_checkouts ORDER BY seen_at')]
            names={r['project_id']:r['name'] for r in db.execute('SELECT * FROM project_names')}
        for row in checkouts:
            key=canonical(row['project_id'],overrides)
            group=groups.setdefault(key,{'id':key,'name':row['name'],'hosts':{},'sessions':[],'last_activity':0})
            if row['project_id']==key:
                group['name']=row['name']
            host=group['hosts'].setdefault(row['machine'],{'machine':row['machine'],'checkouts':[],'allowed_agents':[]})
            host['checkouts'].append({'branch':row['branch'],'worktree':row['worktree']})
        activity=self.activity_times()
        for session in self.sessions():
            key=project_key(session)
            if not key:continue
            key=canonical(key,overrides)
            group=groups.setdefault(key,{'id':key,'name':session['project'],'hosts':{},'sessions':[],'last_activity':0})
            group['sessions'].append(session)
            group['last_activity']=max(group['last_activity'],activity.get((session['source'],session['session']),session['updated_at']))
            if session.get('machine'):
                group['hosts'].setdefault(session['machine'],{'machine':session['machine'],'checkouts':[],'allowed_agents':[]})
        for key,group in groups.items():
            override=overrides.get(key,{})
            group['name']=override.get('name') or names.get(key) or group['name']
            group['hidden']=bool(override.get('hidden'))
            group['hosts']=list(group['hosts'].values())
            group['sessions'].sort(key=lambda s:(s['effective_state'] not in ('working','waiting','blocked'),-s['updated_at']))
            keys={(s['source'],s['session']) for s in group['sessions']}
            usage=self.token_usage(since,session_keys=keys)
            group['tokens']={field:sum(entry[field] for entry in usage.values()) for field in ('input','cached','output','sessions')}
        return sorted(groups.values(),key=lambda group:(-group['last_activity'],group['name']))

    def update_project(self, key, patch):
        if not isinstance(patch,dict) or not patch or set(patch)-{'name','hidden','merge_into'}:
            raise ValueError('invalid override')
        if 'name' in patch and (not isinstance(patch['name'],str) or not patch['name'].strip() or len(patch['name'])>120):
            raise ValueError('invalid name')
        if 'hidden' in patch and type(patch['hidden']) is not bool:
            raise ValueError('invalid hidden flag')
        known={p['id'] for p in self.projects()}
        if key not in known:
            raise LookupError('unknown project')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            overrides={r['project_id']:dict(r) for r in db.execute('SELECT * FROM project_overrides')}
            current=overrides.get(key,{'name':None,'hidden':False,'merge_into':None})
            target=patch.get('merge_into')
            if 'merge_into' in patch:
                if not isinstance(target,str) or target not in known:
                    raise ValueError('unknown merge target')
                if canonical(target,overrides)==key:
                    raise ValueError('cannot merge a project into itself')
                target=canonical(target,overrides)
            db.execute('INSERT OR REPLACE INTO project_overrides VALUES(?,?,?,?)',
                (key,patch.get('name',current['name']),patch.get('hidden',current['hidden']),target or current['merge_into']))
        self.changed()
        return {'updated':True}
