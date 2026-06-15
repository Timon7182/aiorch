import { useState } from 'react';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
  DialogDescription,
} from './ui/dialog';
import { Button } from './ui/button';
import { Label } from './ui/label';
import { Input } from './ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from './ui/select';
import { Loader2 } from 'lucide-react';

interface AddDatabaseModalProps {
  open?: boolean;
  onClose: () => void;
  onSaved: () => void; // called after a successful create so the caller can refresh
}

const DEFAULT_PORTS: Record<string, number> = { postgres: 5432, mysql: 3306, sqlite: 0 };

/**
 * Minimal "add a database connection" form (the chat DB selector reads these).
 * Saves via window.API.createDatabase → POST /api/ext/databases (authenticated),
 * so credentials are entered by the user in their own session.
 */
export function AddDatabaseModal({ open = true, onClose, onSaved }: AddDatabaseModalProps) {
  const [name, setName] = useState('');
  const [kind, setKind] = useState('postgres');
  const [host, setHost] = useState('192.168.88.55');
  const [port, setPort] = useState<string>('5432');
  const [database, setDatabase] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [env, setEnv] = useState('preview');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onKindChange = (k: string) => {
    setKind(k);
    if (DEFAULT_PORTS[k]) setPort(String(DEFAULT_PORTS[k]));
  };

  const handleSave = async () => {
    if (!name.trim() || !database.trim()) {
      setError('Name and database are required.');
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const res = await window.API.createDatabase({
        name: name.trim(),
        kind,
        env: env.trim() || 'dev',
        host: host.trim() || 'localhost',
        port: port ? Number(port) : undefined,
        database: database.trim(),
        username: username.trim() || undefined,
        password: password || undefined,
      });
      if (res?.success) {
        onSaved();
        onClose();
      } else {
        setError(res?.error || 'Failed to save connection');
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Add database connection</DialogTitle>
          <DialogDescription>
            Read-only connection used by the chat's Database selector.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="space-y-1">
            <Label htmlFor="db-name">Name</Label>
            <Input id="db-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="cts-main" />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <Label>Kind</Label>
              <Select value={kind} onValueChange={onKindChange}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="postgres">PostgreSQL</SelectItem>
                  <SelectItem value="mysql">MySQL</SelectItem>
                  <SelectItem value="sqlite">SQLite</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="db-env">Environment</Label>
              <Input id="db-env" value={env} onChange={(e) => setEnv(e.target.value)} placeholder="preview" />
            </div>
          </div>
          <div className="grid grid-cols-3 gap-3">
            <div className="col-span-2 space-y-1">
              <Label htmlFor="db-host">Host</Label>
              <Input id="db-host" value={host} onChange={(e) => setHost(e.target.value)} />
            </div>
            <div className="space-y-1">
              <Label htmlFor="db-port">Port</Label>
              <Input id="db-port" value={port} onChange={(e) => setPort(e.target.value)} />
            </div>
          </div>
          <div className="space-y-1">
            <Label htmlFor="db-database">Database</Label>
            <Input id="db-database" value={database} onChange={(e) => setDatabase(e.target.value)} placeholder="cts_static_a" />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <Label htmlFor="db-user">Username</Label>
              <Input id="db-user" value={username} onChange={(e) => setUsername(e.target.value)} placeholder="admin" />
            </div>
            <div className="space-y-1">
              <Label htmlFor="db-pass">Password</Label>
              <Input id="db-pass" type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
            </div>
          </div>
          {error && <p className="text-xs text-destructive">{error}</p>}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={saving}>Cancel</Button>
          <Button onClick={handleSave} disabled={saving}>
            {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            Save connection
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default AddDatabaseModal;
