import { app, Menu, BrowserWindow, shell } from 'electron'
import type { MenuItemConstructorOptions } from 'electron'

const isMac = process.platform === 'darwin'
const SKILL_HUB_URL = 'https://skills.cowagent.ai/'
const DOCS_URL = 'https://docs.cowagent.ai'

// Send a menu-triggered action to the renderer (e.g. new chat, open settings).
function emit(win: BrowserWindow | null, action: string) {
  win?.webContents.send('menu-action', action)
}

/**
 * Build a minimal, purpose-built application menu. We intentionally drop most of
 * Electron's verbose defaults and keep only items that are actually useful for
 * this app, plus the shortcuts users expect (New Chat, Settings, Reload, etc).
 */
export function buildAppMenu(getWindow: () => BrowserWindow | null) {
  const win = () => getWindow()

  const appMenu: MenuItemConstructorOptions[] = isMac
    ? [
        {
          label: app.name,
          submenu: [
            { role: 'about' },
            { type: 'separator' },
            { label: '设置…', accelerator: 'Cmd+,', click: () => emit(win(), 'open-settings') },
            { type: 'separator' },
            { role: 'hide' },
            { role: 'hideOthers' },
            { role: 'unhide' },
            { type: 'separator' },
            { role: 'quit' },
          ],
        },
      ]
    : []

  const fileMenu: MenuItemConstructorOptions = {
    label: '文件',
    submenu: [
      { label: '新建对话', accelerator: 'CmdOrCtrl+N', click: () => emit(win(), 'new-chat') },
      ...(!isMac
        ? ([
            { label: '设置', accelerator: 'Ctrl+,', click: () => emit(win(), 'open-settings') },
            { type: 'separator' },
            { role: 'quit' },
          ] as MenuItemConstructorOptions[])
        : []),
    ],
  }

  const editMenu: MenuItemConstructorOptions = {
    label: '编辑',
    submenu: [
      { role: 'undo', label: '撤销' },
      { role: 'redo', label: '重做' },
      { type: 'separator' },
      { role: 'cut', label: '剪切' },
      { role: 'copy', label: '复制' },
      { role: 'paste', label: '粘贴' },
      { role: 'selectAll', label: '全选' },
    ],
  }

  const viewMenu: MenuItemConstructorOptions = {
    label: '视图',
    submenu: [
      { role: 'reload', label: '重新加载' },
      { role: 'toggleDevTools', label: '开发者工具' },
      { type: 'separator' },
      { role: 'resetZoom', label: '实际大小' },
      { role: 'zoomIn', label: '放大' },
      { role: 'zoomOut', label: '缩小' },
      { type: 'separator' },
      { role: 'togglefullscreen', label: '切换全屏' },
    ],
  }

  const windowMenu: MenuItemConstructorOptions = {
    label: '窗口',
    submenu: [
      { role: 'minimize', label: '最小化' },
      ...(isMac ? ([{ role: 'zoom' }] as MenuItemConstructorOptions[]) : []),
      { type: 'separator' },
      // Explicit Close so Cmd/Ctrl+W reliably triggers our close-to-tray hide.
      { label: '关闭窗口', accelerator: 'CmdOrCtrl+W', click: () => win()?.close() },
    ],
  }

  const helpMenu: MenuItemConstructorOptions = {
    label: '帮助',
    submenu: [
      { label: '查看日志', click: () => emit(win(), 'view-logs') },
      { type: 'separator' },
      { label: '使用文档', click: () => shell.openExternal(DOCS_URL) },
      { label: '技能中心', click: () => shell.openExternal(SKILL_HUB_URL) },
    ],
  }

  const template: MenuItemConstructorOptions[] = [
    ...appMenu,
    fileMenu,
    editMenu,
    viewMenu,
    windowMenu,
    helpMenu,
  ]

  Menu.setApplicationMenu(Menu.buildFromTemplate(template))
}
