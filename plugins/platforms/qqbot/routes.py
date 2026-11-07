"""Account-scoped QQ destination kinds; never guess the endpoint after restart."""
from contextlib import closing
from pathlib import Path
import sqlite3

from hermes_constants import get_hermes_home


class ChatRoutes:
    def __init__(self, account):
        self.path = Path(get_hermes_home()) / 'qqbot-routes.db'
        self.account = account

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version > 1:
                raise ValueError('QQ routes schema is newer than this adapter')
            with conn:
                conn.execute('CREATE TABLE IF NOT EXISTS routes '
                             '(account TEXT, chat TEXT, kind TEXT, PRIMARY KEY(account,chat))')
                conn.execute('PRAGMA user_version=1')
            return conn
        except BaseException:
            conn.close()
            raise

    def remember(self, chat, kind):
        if kind not in {'c2c', 'group', 'guild', 'dm'} or not self.account:
            raise ValueError('unknown QQ account/chat kind')
        with closing(self._connect()) as conn, conn:
            conn.execute('INSERT INTO routes VALUES (?,?,?) ON CONFLICT(account,chat) DO UPDATE SET kind='
                         "CASE WHEN routes.kind=excluded.kind THEN routes.kind ELSE 'conflict' END",
                         (self.account, chat, kind))

    def get(self, chat):
        if not self.path.exists() or not self.account:
            return None
        with closing(self._connect()) as conn:
            row = conn.execute('SELECT kind FROM routes WHERE account=? AND chat=?', (self.account, chat)).fetchone()
        return row[0] if row else None
