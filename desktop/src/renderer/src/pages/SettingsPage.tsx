import React, { useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { Bell, Bot, Cpu, Settings2, ShieldCheck, SlidersHorizontal, X } from 'lucide-react'
import { product } from '@product'
import BasicSettings, { type BasicSettingsSection } from './settings/BasicSettings'
import ModelsTab from './settings/ModelsTab'

interface SettingsPageProps {
  baseUrl: string
  onLangChange?: () => void
}

type Section = BasicSettingsSection | 'model-capabilities'

const SettingsPage: React.FC<SettingsPageProps> = ({ baseUrl, onLangChange }) => {
  const location = useLocation()
  const navigate = useNavigate()
  const modelsTabHidden = product.models?.hideModelsTab === true
  const initial: Section = location.pathname === '/models' || new URLSearchParams(location.search).get('tab') === 'models'
    ? modelsTabHidden ? 'model' : 'model-capabilities'
    : 'general'
  const [section, setSection] = useState<Section>(initial)

  useEffect(() => {
    setSection(initial)
  }, [location.pathname, location.search])

  const groups: { label: string; items: { key: Section; label: string; icon: React.ReactNode }[] }[] = [
    {
      label: '常规',
      items: [
        { key: 'general', label: '通用', icon: <Settings2 size={16} /> },
        { key: 'notifications', label: '通知', icon: <Bell size={16} /> },
      ],
    },
    {
      label: '功能',
      items: [
        { key: 'model', label: '模型', icon: <Cpu size={16} /> },
        { key: 'agent', label: 'Agent', icon: <Bot size={16} /> },
        ...(!modelsTabHidden ? [{ key: 'model-capabilities' as Section, label: '模型能力', icon: <SlidersHorizontal size={16} /> }] : []),
      ],
    },
    {
      label: '数据与安全',
      items: [{ key: 'security', label: '权限与安全', icon: <ShieldCheck size={16} /> }],
    },
  ]

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/35 p-3 sm:p-5" role="dialog" aria-modal="true" aria-label="设置">
      <div className="relative flex h-full max-h-[900px] w-full max-w-[1060px] overflow-hidden rounded-card border border-default bg-base shadow-xl">
        <aside className="w-[210px] shrink-0 overflow-y-auto border-r border-default bg-surface-2 px-3 py-6 max-sm:w-[64px] max-sm:px-2">
          <div className="mb-4 px-3 text-sm font-semibold text-content max-sm:sr-only">设置</div>
          {groups.map((group) => (
            <div key={group.label} className="mb-5">
              <div className="mb-1 px-3 text-xs text-content-tertiary max-sm:sr-only">{group.label}</div>
              <div className="space-y-0.5">
                {group.items.map((item) => (
                  <button
                    key={item.key}
                    type="button"
                    title={item.label}
                    aria-current={section === item.key ? 'page' : undefined}
                    onClick={() => setSection(item.key)}
                    className={`flex w-full items-center gap-2.5 rounded-btn px-3 py-2 text-left text-sm transition-colors cursor-pointer max-sm:justify-center max-sm:px-2 ${
                      section === item.key ? 'bg-surface text-content font-medium shadow-sm' : 'text-content-secondary hover:bg-surface hover:text-content'
                    }`}
                  >
                    {item.icon}
                    <span className="max-sm:sr-only">{item.label}</span>
                  </button>
                ))}
              </div>
            </div>
          ))}
        </aside>

        <main className="min-w-0 flex-1 overflow-y-auto px-5 pb-8 pt-8 sm:px-8">
          <div className="mx-auto max-w-[720px]">
            {section === 'model-capabilities' ? (
              <ModelsTab baseUrl={baseUrl} />
            ) : (
              <BasicSettings
                baseUrl={baseUrl}
                section={section}
                onLangChange={onLangChange}
                onOpenModels={modelsTabHidden ? undefined : () => setSection('model-capabilities')}
              />
            )}
          </div>
        </main>
        <button
          type="button"
          aria-label="关闭设置"
          title="关闭设置"
          onClick={() => navigate('/chat')}
          className="absolute right-3 top-3 z-10 rounded-btn p-1.5 text-content-tertiary hover:bg-surface-2 hover:text-content cursor-pointer"
        >
          <X size={18} />
        </button>
      </div>
    </div>
  )
}

export default SettingsPage
