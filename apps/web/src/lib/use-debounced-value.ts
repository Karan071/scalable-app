import { useEffect, useState } from 'react'

/** LLD-15 §5: 300ms after the last keystroke. */
const DEBOUNCE_MS = 300

export function useDebouncedValue<T>(value: T, delayMs: number = DEBOUNCE_MS): T {
  const [debounced, setDebounced] = useState(value)

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs)
    return () => clearTimeout(timer)
  }, [value, delayMs])

  return debounced
}
