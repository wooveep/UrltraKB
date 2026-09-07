// A separate thread can stop a CPU-bound MathJax main thread after owner loss.
import { Worker } from 'node:worker_threads';

export function startParentGuard() {
  const parent = Number(process.env.OPENKB_RENDER_PARENT_PID || process.ppid);
  const lifetime = Math.min(60000, Math.max(100, Number(process.env.OPENKB_RENDER_TIMEOUT_MS) || 60000));
  const ready = new Int32Array(new SharedArrayBuffer(4));
  const guard = new Worker(`
    process.on('uncaughtException', () => process.kill(process.pid, 'SIGKILL'));
    const { workerData } = require('node:worker_threads');
    const fs = require('node:fs');
    const deadline = Date.now() + workerData.lifetime;
    function identity() {
      const stat = fs.readFileSync('/proc/' + workerData.parent + '/stat', 'utf8');
      const fields = stat.slice(stat.lastIndexOf(')') + 2).split(' ');
      if (fields[0] === 'Z') throw new Error('owner exited');
      return fields[19];
    }
    const expected = process.env.OPENKB_RENDER_PARENT_START;
    function check() {
      try {
        process.kill(workerData.parent, 0);
        if (process.platform === 'linux' && expected && identity() !== expected) {
          throw new Error('owner changed');
        }
        if (Date.now() >= deadline) throw new Error('render deadline');
      } catch (error) {
        // Only this helper is terminated. No signal is sent to the owner.
        process.kill(process.pid, 'SIGKILL');
      }
    }
    check();
    setInterval(check, 100);
    const ready = new Int32Array(workerData.ready);
    Atomics.store(ready, 0, 1);
    Atomics.notify(ready, 0);
  `, { eval: true, execArgv: [], workerData: { parent, lifetime, ready: ready.buffer } });
  guard.on('error', () => process.kill(process.pid, 'SIGKILL'));
  // Do not enter synchronous rendering unless the independent guard started.
  Atomics.wait(ready, 0, 0, 2000);
  if (Atomics.load(ready, 0) !== 1) process.kill(process.pid, 'SIGKILL');
  guard.unref();
}
