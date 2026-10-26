import { create } from 'zustand'

const NAV_KEY = 'cow_nav_collapsed'
const SESSIONS_KEY = 'cow_sessions_collapsed'
const TASK_NOTIFY_KEY = 'cow_task_notify'
const TASK_NOTIFY_SOUND_KEY = 'cow_task_notify_sound'
const FONT_SCALE_KEY = 'cow_font_scale'

function readFontScale(): number {
  const stored = localStorage.getItem(FONT_SCALE_KEY)
  if (stored === null) return 1
  const value = Number(stored)
  return Number.isFinite(value) && value >= 0 && value <= 4 ? value : 1
}

function applyFontScale(value: number): void {
  if (typeof document !== 'undefined') {
    document.documentElement.style.fontSize = `${[14.4, 16, 16.8, 17.6, 19.2][value] || 16}px`
  }
}

const initialFontScale = readFontScale()
applyFontScale(initialFontScale)

interface UIState {
  /** Navigation rail collapsed (icon-only) vs expanded (icon + label). */
  navCollapsed: boolean
  toggleNav: () => void
  setNavCollapsed: (v: boolean) => void

  /** Session list panel collapsed (hidden) vs expanded. */
  sessionsCollapsed: boolean
  toggleSessions: () => void
  setSessionsCollapsed: (v: boolean) => void

  /** OS notification when an agent run finishes (done or error). */
  taskNotify: boolean
  setTaskNotify: (v: boolean) => void
  /** Play the notification sound; only effective while taskNotify is on. */
  taskNotifySound: boolean
  setTaskNotifySound: (v: boolean) => void

  fontScale: number
  setFontScale: (v: number) => void

  /** Currently active session id (Chat page). */
  activeSessionId: string | null
  setActiveSessionId: (id: string | null) => void
}

function readBool(key: string, fallback = false): boolean {
  const v = localStorage.getItem(key)
  return v === null ? fallback : v === '1'
}

export const useUIStore = create<UIState>((set) => ({
  navCollapsed: readBool(NAV_KEY),
  toggleNav: () =>
    set((s) => {
      const next = !s.navCollapsed
      localStorage.setItem(NAV_KEY, next ? '1' : '0')
      return { navCollapsed: next }
    }),
  setNavCollapsed: (v) => {
    localStorage.setItem(NAV_KEY, v ? '1' : '0')
    set({ navCollapsed: v })
  },

  sessionsCollapsed: readBool(SESSIONS_KEY),
  toggleSessions: () =>
    set((s) => {
      const next = !s.sessionsCollapsed
      localStorage.setItem(SESSIONS_KEY, next ? '1' : '0')
      return { sessionsCollapsed: next }
    }),
  setSessionsCollapsed: (v) => {
    localStorage.setItem(SESSIONS_KEY, v ? '1' : '0')
    set({ sessionsCollapsed: v })
  },

  taskNotify: readBool(TASK_NOTIFY_KEY, true),
  setTaskNotify: (v) => {
    localStorage.setItem(TASK_NOTIFY_KEY, v ? '1' : '0')
    set({ taskNotify: v })
  },
  taskNotifySound: readBool(TASK_NOTIFY_SOUND_KEY, true),
  setTaskNotifySound: (v) => {
    localStorage.setItem(TASK_NOTIFY_SOUND_KEY, v ? '1' : '0')
    set({ taskNotifySound: v })
  },

  fontScale: initialFontScale,
  setFontScale: (v) => {
    const next = Math.max(0, Math.min(4, Math.round(v)))
    localStorage.setItem(FONT_SCALE_KEY, String(next))
    applyFontScale(next)
    set({ fontScale: next })
  },

  activeSessionId: null,
  setActiveSessionId: (id) => set({ activeSessionId: id }),
}))
