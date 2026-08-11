import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const files = [
  path.join(root, 'node_modules', 'simple-git', 'dist', 'cjs', 'index.js'),
  path.join(root, 'node_modules', 'simple-git', 'dist', 'esm', 'index.js')
]
const before = `    if (allowUnsafe) {
      console.warn(WRONG_CHARS_ERR);
    } else {
      throw new GitPluginError(void 0, "binary", WRONG_CHARS_ERR);
    }`
const after = `    if (!allowUnsafe) {
      throw new GitPluginError(void 0, "binary", WRONG_CHARS_ERR);
    }`

for (const file of files) {
  if (!fs.existsSync(file)) {
    continue
  }

  const source = fs.readFileSync(file, 'utf8')

  if (source.includes(after)) {
    continue
  }

  if (!source.includes(before)) {
    throw new Error(`simple-git patch target changed: ${file}`)
  }

  fs.writeFileSync(file, source.replace(before, after))
}