"""Invalidate retrieval caches for source/vector changes, not file-stat bookkeeping."""


def install(db):
    db.execute('CREATE TABLE IF NOT EXISTS retrieval_counters(key TEXT PRIMARY KEY,value INTEGER)')
    db.execute("DELETE FROM metadata WHERE key IN ('retrieval_revision','vector_revision')")
    for table, key, fields in (
            ('nodes','retrieval_revision',('body','request_mapping','path','name','kind','start','end')),
            ('embeddings','vector_revision',('node_id','chunk','model','digest','vector'))):
        db.execute('INSERT OR IGNORE INTO retrieval_counters VALUES (?,?)', (key,0))
        for action in ('INSERT','DELETE','UPDATE'):
            condition = (' WHEN '+ ' OR '.join(f'OLD.{field} IS NOT NEW.{field}' for field in fields)
                         if action == 'UPDATE' else '')
            old = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                             (f'{table}_{action.lower()}_revision',)).fetchone()
            if old and 'UPDATE metadata' in old[0]:
                db.execute(f'DROP TRIGGER {table}_{action.lower()}_revision')
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()}_revision
                AFTER {action} ON {table}{condition} BEGIN
                UPDATE retrieval_counters SET value=value+1 WHERE key='{key}'; END''')
    db.commit()
