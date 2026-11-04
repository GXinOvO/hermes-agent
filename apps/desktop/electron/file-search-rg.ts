import { spawn } from 'node:child_process'

/** Resolve PM's selected executable without installing a package or starting a
 * gateway. The existing runtime resolver owns the interpreter/installation. */
export function resolveNativeSearchRg(
  runtime: { command: string; env?: NodeJS.ProcessEnv },
  cwd: string,
  signal: AbortSignal
): Promise<string> {
  const script = [
    'import pm',
    'from hermes_platform.resolver import locate_command',
    'p = pm.installed_package("ripgrep")',
    'c = locate_command("rg").command if not (p and p.binary) else None',
    'print(str(p.binary) if p and p.binary else (c[0] if c else ""))'
  ].join('\n')

  return new Promise((resolve, reject) => {
    const child = spawn(runtime.command, ['-c', script], {
      cwd,
      env: { ...process.env, ...runtime.env, HERMES_DISABLE_LAZY_INSTALLS: '1' },
      shell: false,
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'ignore']
    })

    let output = ''
    const abort = () => child.kill('SIGKILL')
    const timer = setTimeout(abort, 5000)
    child.stdout.setEncoding('utf8')
    child.stdout.on('data', data => {
      output += data

      if (output.length > 32768) {
        abort()
      }
    })
    child.on('error', reject)
    signal.addEventListener('abort', abort, { once: true })

    if (signal.aborted) {
      abort()
    }

    child.on('close', code => {
      clearTimeout(timer)
      signal.removeEventListener('abort', abort)

      if (signal.aborted) {
        reject(Object.assign(new Error('Search cancelled'), { name: 'AbortError' }))
      } else if (code !== 0 || !output.trim()) {
        reject(new Error('ripgrep is unavailable on the local file host'))
      } else {
        resolve(output.trim())
      }
    })
  })
}
