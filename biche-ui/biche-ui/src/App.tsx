import React, { useState, useEffect, useRef } from 'react';

// Types
type ShotStatus = 'pending' | 'generating' | 'review' | 'approved' | 'rejected' | 'unsatisfactory';

interface GeneratedImage {
  url: string;
  timestamp: Date;
}

interface Shot {
  id: number;
  status: ShotStatus;
  sceneDescription: string;
  referenceImage: string | null;
  keyframePrompt: string;
  firstFramePrompt: string;
  lastFramePrompt: string;
  keyframeImage: GeneratedImage | null;
  firstFrameImage: GeneratedImage | null;
  lastFrameImage: GeneratedImage | null;
  videoUrl: string | null;
  satisfaction: 'satisfied' | 'unsatisfied' | null;
}

// Mock Data
const initialShots: Shot[] = [
  {
    id: 1,
    status: 'approved',
    sceneDescription: '城市天际线在晨曦中渐渐亮起，高楼玻璃幕墙反射着第一缕阳光',
    referenceImage: 'https://picsum.photos/seed/shot1ref/400/225',
    keyframePrompt: '城市天际线航拍视角，太阳刚刚升起，金色光线穿过摩天大楼间隙，晨雾弥漫，8K电影质感',
    firstFramePrompt: '城市黎明时分，摄像机从高空俯瞰，城市灯光刚刚熄灭，第一缕阳光穿透云层',
    lastFramePrompt: '城市完全被阳光照亮，阳光直射镜头产生光晕效果，温暖的金色色调',
    keyframeImage: { url: 'https://picsum.photos/seed/shot1kf/400/225', timestamp: new Date() },
    firstFrameImage: { url: 'https://picsum.photos/seed/shot1ff/400/225', timestamp: new Date() },
    lastFrameImage: { url: 'https://picsum.photos/seed/shot1lf/400/225', timestamp: new Date() },
    videoUrl: null,
    satisfaction: 'satisfied',
  },
  {
    id: 2,
    status: 'review',
    sceneDescription: '年轻女主站在地铁站台上，霓虹灯光映照着她若有所思的表情',
    referenceImage: null,
    keyframePrompt: '地铁站内景，深景深，霓虹灯牌发出蓝紫色光芒，光线尘埃漂浮',
    firstFramePrompt: '地铁站台中景，女主侧身站立，穿着深色风衣，目光凝视远方，霓虹倒影',
    lastFramePrompt: '地铁进站，灯光扫过站台，列车带动的风吹起女主发丝，戏剧性瞬间',
    keyframeImage: { url: 'https://picsum.photos/seed/shot2kf/400/225', timestamp: new Date() },
    firstFrameImage: { url: 'https://picsum.photos/seed/shot2ff/400/225', timestamp: new Date() },
    lastFrameImage: { url: 'https://picsum.photos/seed/shot2lf/400/225', timestamp: new Date() },
    videoUrl: null,
    satisfaction: null,
  },
  {
    id: 3,
    status: 'generating',
    sceneDescription: '男主在雨中奔跑，脚步溅起水花，路灯下的雨丝如银线般闪烁',
    referenceImage: 'https://picsum.photos/seed/shot3ref/400/225',
    keyframePrompt: '城市街道雨中夜景，slow motion效果，雨滴在路灯下形成光点，速度感',
    firstFramePrompt: '雨中街道全景，男主从远处跑来，脚步声溅起水花，路灯投下暖黄色光晕',
    lastFramePrompt: '男主跑过镜头，水花飞溅，慢动作特写，雨滴在空气中悬浮闪烁',
    keyframeImage: null,
    firstFrameImage: null,
    lastFrameImage: null,
    videoUrl: null,
    satisfaction: null,
  },
  {
    id: 4,
    status: 'pending',
    sceneDescription: '老旧电影院的霓虹招牌在夜色中闪烁，放映厅内传来模糊的经典台词',
    referenceImage: null,
    keyframePrompt: '',
    firstFramePrompt: '',
    lastFramePrompt: '',
    keyframeImage: null,
    firstFrameImage: null,
    lastFrameImage: null,
    videoUrl: null,
    satisfaction: null,
  },
  {
    id: 5,
    status: 'rejected',
    sceneDescription: '咖啡厅角落座位，阳光透过窗户洒在桌上的书本和咖啡杯上',
    referenceImage: null,
    keyframePrompt: '咖啡厅窗边角落，温暖的阳光角度，书页被风吹动，咖啡蒸汽袅袅升起',
    firstFramePrompt: '木质桌面特写，阳光斜射，手捧咖啡杯，书本翻开，放射状光线',
    lastFramePrompt: '书页被微风轻轻吹动，阳光透过窗帘形成丁达尔效应，温暖氛围',
    keyframeImage: { url: 'https://picsum.photos/seed/shot5kf/400/225', timestamp: new Date() },
    firstFrameImage: { url: 'https://picsum.photos/seed/shot5ff/400/225', timestamp: new Date() },
    lastFrameImage: { url: 'https://picsum.photos/seed/shot5lf/400/225', timestamp: new Date() },
    videoUrl: null,
    satisfaction: 'unsatisfied',
  },
  {
    id: 6,
    status: 'unsatisfactory',
    sceneDescription: '天台夜景，城市的万家灯火如星海般闪烁，远方传来隐约的车流声',
    referenceImage: 'https://picsum.photos/seed/shot6ref/400/225',
    keyframePrompt: '城市天台俯拍，夜景模式，长曝光车流形成光线，星光点点的高楼窗户',
    firstFramePrompt: '天台边缘镜头，男子背影，城市全景在他脚下展开，夜风轻拂',
    lastFramePrompt: '城市灯火阑珊处突然绽放烟花，照亮整个天台和男子脸庞，情感瞬间',
    keyframeImage: { url: 'https://picsum.photos/seed/shot6kf/400/225', timestamp: new Date() },
    firstFrameImage: { url: 'https://picsum.photos/seed/shot6ff/400/225', timestamp: new Date() },
    lastFrameImage: { url: 'https://picsum.photos/seed/shot6lf/400/225', timestamp: new Date() },
    videoUrl: null,
    satisfaction: 'unsatisfied',
  },
];

// Workflow Steps
const workflowSteps = [
  { label: '启动', description: '创建分镜表格' },
  { label: '填写&优化', description: '输入镜头信息' },
  { label: '并行生成帧', description: 'AI生成关键帧' },
  { label: '审核&生成', description: '审核并生成视频' },
  { label: '归档', description: '整理归档' },
];

const statusLabels: Record<ShotStatus, string> = {
  pending: '待生成',
  generating: '生成中',
  review: '待审核',
  approved: '通过',
  rejected: '驳回',
  unsatisfactory: '不满意',
};

const statusFilters = ['全部', '待生成', '生成中', '待审核', '通过', '驳回', '不满意'];

// Icons
const Icons = {
  Film: () => (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="2" y="2" width="20" height="20" rx="2" />
      <path d="M7 2v20M17 2v20M2 12h20M2 7h5M2 17h5M17 17h5M17 7h5" />
    </svg>
  ),
  Sparkle: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M12 3v18M3 12h18M5.6 5.6l12.8 12.8M18.4 5.6L5.6 18.4" />
    </svg>
  ),
  Image: () => (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <circle cx="8.5" cy="8.5" r="1.5" />
      <path d="M21 15l-5-5L5 21" />
    </svg>
  ),
  Plus: () => (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M12 5v14M5 12h14" />
    </svg>
  ),
  Download: () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M7 10l5 5 5-5M12 15V3" />
    </svg>
  ),
  Play: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
      <polygon points="5,3 19,12 5,21" />
    </svg>
  ),
  Check: () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3">
      <polyline points="20,6 9,17 4,12" />
    </svg>
  ),
  X: () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3">
      <path d="M18 6L6 18M6 6l12 12" />
    </svg>
  ),
  Wand: () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M15 4V2M15 16v-2M8 9h2M20 9h2M17.8 11.8L19 13M17.8 6.2L19 5M12.2 11.8L11 13M12.2 6.2L11 5" />
      <rect x="3" y="8" width="14" height="8" rx="1" />
    </svg>
  ),
  ChevronRight: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <polyline points="9,18 15,12 9,6" />
    </svg>
  ),
  ChevronDown: () => (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <polyline points="6,9 12,15 18,9" />
    </svg>
  ),
  Settings: () => (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-2 2 2 2 0 01-2-2v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83 0 2 2 0 010-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 01-2-2 2 2 0 012-2h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 010-2.83 2 2 0 012.83 0l.06.06a1.65 1.65 0 001.82.33H9a1.65 1.65 0 001-1.51V3a2 2 0 012-2 2 2 0 012 2v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 0 2 2 0 010 2.83l-.06.06a1.65 1.65 0 00-.33 1.82V9a1.65 1.65 0 001.51 1H21a2 2 0 012 2 2 2 0 01-2 2h-.09a1.65 1.65 0 00-1.51 1z" />
    </svg>
  ),
  User: () => (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2" />
      <circle cx="12" cy="7" r="4" />
    </svg>
  ),
  Close: () => (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M18 6L6 18M6 6l12 12" />
    </svg>
  ),
};

// Top Navigation Component
const TopNav: React.FC<{ currentStep: number; onStepChange: (step: number) => void }> = ({ currentStep, onStepChange }) => (
  <header className="fixed top-0 left-0 right-0 z-50 glass border-b border-amber/10">
    <div className="flex items-center justify-between px-6 py-4">
      {/* Brand Logo */}
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-lg bg-gradient-to-br from-amber to-amber-dark flex items-center justify-center">
            <Icons.Film />
          </div>
          <div>
            <h1 className="font-serif text-xl font-bold text-cream tracking-wide">哔车</h1>
            <p className="text-[10px] text-cream-muted font-mono tracking-widest">AICINE STUDIO</p>
          </div>
        </div>
        <div className="h-8 w-px bg-white/10 mx-2" />
        <div>
          <p className="text-sm text-cream font-medium">《城市夜未眠》</p>
          <p className="text-[10px] text-cream-muted">都市情感剧 · 第3集</p>
        </div>
      </div>

      {/* Workflow Steps */}
      <div className="flex items-center gap-2">
        {workflowSteps.map((step, index) => (
          <React.Fragment key={step.label}>
            <button
              onClick={() => onStepChange(index)}
              className={`workflow-step group flex items-center gap-2 px-3 py-2 rounded-lg transition-all duration-300 ${
                currentStep === index
                  ? 'bg-amber/10 text-amber'
                  : 'text-cream-muted hover:bg-white/5 hover:text-cream'
              }`}
            >
              <span className={`workflow-dot ${currentStep >= index ? (currentStep === index ? 'active' : 'completed') : ''}`} />
              <span className="text-sm font-medium whitespace-nowrap">{step.label}</span>
            </button>
            {index < workflowSteps.length - 1 && (
              <div className={`workflow-line ${currentStep > index ? 'bg-amber/30' : ''}`} />
            )}
          </React.Fragment>
        ))}
      </div>

      {/* User & Settings */}
      <div className="flex items-center gap-3">
        <button className="btn-icon">
          <Icons.Settings />
        </button>
        <div className="w-8 h-8 rounded-full bg-gradient-to-br from-amber to-amber-dark flex items-center justify-center">
          <Icons.User />
        </div>
      </div>
    </div>
    {/* Subtle gradient underline */}
    <div className="absolute bottom-0 left-0 right-0 h-px bg-gradient-to-r from-transparent via-amber/30 to-transparent" />
  </header>
);

// Shot Card Component
const ShotCard: React.FC<{
  shot: Shot;
  index: number;
  onSelect: () => void;
  onUpdate: (updated: Partial<Shot>) => void;
}> = ({ shot, index, onSelect, onUpdate }) => {
  const [expandedPrompts, setExpandedPrompts] = useState<Record<string, boolean>>({});
  const [isGenerating, setIsGenerating] = useState(false);

  const togglePrompt = (key: string) => {
    setExpandedPrompts(prev => ({ ...prev, [key]: !prev[key] }));
  };

  const handleGenerate = () => {
    setIsGenerating(true);
    onUpdate({ status: 'generating' });
    // Simulate generation
    setTimeout(() => {
      setIsGenerating(false);
      onUpdate({
        status: 'review',
        keyframeImage: { url: `https://picsum.photos/seed/shot${shot.id}kf${Date.now()}/400/225`, timestamp: new Date() },
        firstFrameImage: { url: `https://picsum.photos/seed/shot${shot.id}ff${Date.now()}/400/225`, timestamp: new Date() },
        lastFrameImage: { url: `https://picsum.photos/seed/shot${shot.id}lf${Date.now()}/400/225`, timestamp: new Date() },
      });
    }, 3000);
  };

  const handleApprove = () => {
    onUpdate({ status: 'approved', satisfaction: 'satisfied' });
  };

  const handleReject = () => {
    onUpdate({ status: 'rejected', satisfaction: 'unsatisfied' });
  };

  const getStatusClass = (status: ShotStatus) => {
    const classes: Record<ShotStatus, string> = {
      pending: 'status-pending',
      generating: 'status-generating',
      review: 'status-review',
      approved: 'status-approved',
      rejected: 'status-rejected',
      unsatisfactory: 'status-unsatisfactory',
    };
    return classes[status];
  };

  const promptTypes = [
    { key: 'keyframe', label: '关键帧提示词', value: shot.keyframePrompt },
    { key: 'firstFrame', label: '首帧提示词', value: shot.firstFramePrompt },
    { key: 'lastFrame', label: '尾帧提示词', value: shot.lastFramePrompt },
  ];

  const imageTypes = [
    { key: 'keyframeImage', label: '关键帧' },
    { key: 'firstFrameImage', label: '首帧' },
    { key: 'lastFrameImage', label: '尾帧' },
  ];

  return (
    <div
      className={`shot-card glass rounded-xl overflow-hidden transition-all duration-300 ${
        isGenerating ? 'generating-glow' : ''
      } ${shot.status === 'approved' ? 'border-l-4 border-l-status-approved' : ''} ${
        shot.status === 'rejected' || shot.status === 'unsatisfactory' ? 'opacity-75' : ''
      }`}
      style={{ animationDelay: `${index * 100}ms` }}
    >
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-white/5">
        <div className="flex items-center gap-3">
          <span className="font-mono text-sm font-semibold text-amber bg-amber/10 px-2 py-1 rounded">
            #{shot.id.toString().padStart(2, '0')}
          </span>
          <span className={`status-badge ${getStatusClass(shot.status)}`}>
            {statusLabels[shot.status]}
          </span>
        </div>
        <button onClick={onSelect} className="btn-icon text-xs">
          <Icons.ChevronRight />
        </button>
      </div>

      {/* Scene Description */}
      <div className="px-4 py-3 border-b border-white/5">
        <textarea
          value={shot.sceneDescription}
          onChange={(e) => onUpdate({ sceneDescription: e.target.value })}
          placeholder="输入场景描述..."
          className="input-field text-sm resize-none"
          rows={2}
        />
      </div>

      {/* Prompts Section */}
      <div className="px-4 py-3 space-y-2">
        {promptTypes.map(({ key, label, value }) => (
          <div key={key} className="bg-black/20 rounded-lg p-3">
            <div className="flex items-center justify-between mb-2">
              <span className="text-[10px] font-mono text-cream-muted uppercase tracking-wider">{label}</span>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => togglePrompt(key)}
                  className="text-xs text-cream-muted hover:text-amber transition-colors"
                >
                  {expandedPrompts[key] ? '收起' : '展开'}
                </button>
                <button className="p-1 rounded hover:bg-amber/10 text-cream-muted hover:text-amber transition-colors">
                  <Icons.Wand />
                </button>
              </div>
            </div>
            <textarea
              value={value}
              onChange={(e) => onUpdate({ [key]: e.target.value } as Partial<Shot>)}
              placeholder={`输入${label}...`}
              className="input-field text-[11px]"
              rows={expandedPrompts[key] ? 3 : 1}
            />
          </div>
        ))}
      </div>

      {/* Reference Image */}
      <div className="px-4 py-3 border-t border-white/5">
        <div className="flex items-center justify-between mb-2">
          <span className="text-[10px] font-mono text-cream-muted uppercase tracking-wider">参考图</span>
        </div>
        <div
          className={`image-slot ${shot.referenceImage ? 'has-image' : ''}`}
          onClick={() => {}}
        >
          {shot.referenceImage ? (
            <img src={shot.referenceImage} alt="Reference" className="rounded" />
          ) : (
            <>
              <Icons.Image />
              <span className="text-cream-muted text-xs mt-2">点击上传参考图</span>
            </>
          )}
        </div>
      </div>

      {/* Generated Images */}
      <div className="px-4 py-3 border-t border-white/5">
        <div className="flex items-center justify-between mb-2">
          <span className="text-[10px] font-mono text-cream-muted uppercase tracking-wider">生成结果</span>
          {isGenerating && (
            <span className="text-xs text-amber animate-pulse">生成中...</span>
          )}
        </div>
        <div className="grid grid-cols-3 gap-2">
          {imageTypes.map(({ key, label }) => {
            const image = shot[key as keyof Shot] as GeneratedImage | null;
            return (
              <div key={key} className={`image-slot ${image ? 'has-image' : ''} relative group`}>
                {image ? (
                  <>
                    <img src={image.url} alt={label} />
                    <div className="absolute inset-0 bg-black/50 opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center gap-2">
                      <button className="btn-icon bg-white/10 backdrop-blur">
                        <Icons.Download />
                      </button>
                    </div>
                    <span className="absolute bottom-1 left-1 text-[8px] font-mono bg-black/60 px-1 rounded">
                      {label}
                    </span>
                  </>
                ) : (
                  <>
                    <div className="w-6 h-6 rounded-full border border-dashed border-white/20 flex items-center justify-center">
                      <span className="text-[8px] font-mono text-cream-muted">{label[0]}</span>
                    </div>
                    <span className="text-[8px] text-cream-muted mt-1">{label}</span>
                  </>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* Action Buttons */}
      <div className="px-4 py-3 border-t border-white/5 flex gap-2 flex-wrap">
        {shot.status === 'pending' || shot.status === 'generating' ? (
          <button
            onClick={handleGenerate}
            disabled={isGenerating}
            className={`btn-primary flex-1 text-sm ${isGenerating ? 'opacity-50 cursor-not-allowed' : ''}`}
          >
            {isGenerating ? '生成中...' : '生成帧'}
          </button>
        ) : shot.status === 'review' ? (
          <>
            <button onClick={handleApprove} className="btn-primary flex-1 text-sm flex items-center justify-center gap-1">
              <Icons.Check />
              通过
            </button>
            <button onClick={handleReject} className="btn-secondary text-sm flex items-center justify-center gap-1">
              <Icons.X />
              驳回
            </button>
          </>
        ) : shot.status === 'approved' ? (
          <>
            <button className="btn-primary flex-1 text-sm flex items-center justify-center gap-1">
              <Icons.Play />
              生成视频
            </button>
            <button className="btn-secondary text-sm flex items-center justify-center gap-1">
              <Icons.Download />
            </button>
          </>
        ) : (
          <button onClick={handleGenerate} className="btn-secondary flex-1 text-sm">
            重新生成
          </button>
        )}
      </div>

      {/* Scan Line Animation for Generating State */}
      {isGenerating && <div className="scan-overlay" />}
    </div>
  );
};

// Detail Panel Drawer
const DetailPanel: React.FC<{
  shot: Shot;
  onClose: () => void;
  onUpdate: (updated: Partial<Shot>) => void;
}> = ({ shot, onClose, onUpdate }) => {
  const [activeTab, setActiveTab] = useState<'preview' | 'prompts' | 'settings'>('preview');

  const tabs = [
    { key: 'preview', label: '预览' },
    { key: 'prompts', label: '提示词' },
    { key: 'settings', label: '设置' },
  ];

  const promptTypes = [
    { key: 'keyframe', label: '关键帧提示词', value: shot.keyframePrompt },
    { key: 'firstFrame', label: '首帧提示词', value: shot.firstFramePrompt },
    { key: 'lastFrame', label: '尾帧提示词', value: shot.lastFramePrompt },
  ];

  const imageTypes = [
    { key: 'keyframeImage', label: '关键帧', image: shot.keyframeImage },
    { key: 'firstFrameImage', label: '首帧', image: shot.firstFrameImage },
    { key: 'lastFrameImage', label: '尾帧', image: shot.lastFrameImage },
  ];

  return (
    <>
      <div className="drawer-overlay" onClick={onClose} />
      <div className="drawer-panel">
        {/* Header */}
        <div className="sticky top-0 bg-bg-surface border-b border-amber/10 px-6 py-4 flex items-center justify-between z-10">
          <div className="flex items-center gap-4">
            <span className="font-mono text-lg font-bold text-amber">
              镜 {shot.id.toString().padStart(2, '0')}
            </span>
            <span className={`status-badge ${shot.status}`}>{statusLabels[shot.status]}</span>
          </div>
          <button onClick={onClose} className="btn-icon">
            <Icons.Close />
          </button>
        </div>

        {/* Tabs */}
        <div className="flex border-b border-white/10 px-6">
          {tabs.map(({ key, label }) => (
            <button
              key={key}
              onClick={() => setActiveTab(key as typeof activeTab)}
              className={`px-4 py-3 text-sm font-medium transition-all border-b-2 -mb-px ${
                activeTab === key
                  ? 'text-amber border-amber'
                  : 'text-cream-muted border-transparent hover:text-cream'
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        {/* Content */}
        <div className="p-6">
          {activeTab === 'preview' && (
            <div className="space-y-4">
              {/* Scene Description */}
              <div className="bg-black/30 rounded-lg p-4">
                <label className="text-[10px] font-mono text-cream-muted uppercase tracking-wider mb-2 block">
                  场景描述
                </label>
                <p className="text-sm text-cream leading-relaxed">{shot.sceneDescription}</p>
              </div>

              {/* Image Previews */}
              <div className="space-y-3">
                {imageTypes.map(({ key, label, image }) => (
                  <div key={key} className="relative group">
                    <label className="text-[10px] font-mono text-cream-muted uppercase tracking-wider mb-2 block">
                      {label}
                    </label>
                    <div className={`image-slot ${image ? 'has-image' : ''}`} style={{ aspectRatio: '16/9' }}>
                      {image ? (
                        <>
                          <img src={image.url} alt={label} className="w-full h-full object-cover" />
                          <div className="absolute inset-0 bg-gradient-to-t from-black/60 to-transparent" />
                          <div className="absolute bottom-2 left-2 right-2 flex items-center justify-between">
                            <span className="text-xs text-cream font-mono">{label}</span>
                            <div className="flex gap-1">
                              <button className="btn-icon bg-white/10 backdrop-blur">
                                <Icons.Download />
                              </button>
                              <button className="btn-icon bg-white/10 backdrop-blur">
                                <Icons.Play />
                              </button>
                            </div>
                          </div>
                        </>
                      ) : (
                        <>
                          <div className="w-8 h-8 rounded-full border border-dashed border-white/30 flex items-center justify-center">
                            <span className="text-sm font-mono text-cream-muted">?</span>
                          </div>
                          <span className="text-xs text-cream-muted mt-2">等待生成</span>
                        </>
                      )}
                    </div>
                  </div>
                ))}
              </div>

              {/* Video Preview Placeholder */}
              {shot.status === 'approved' && (
                <div className="bg-black/30 rounded-lg p-6 text-center">
                  <div className="w-16 h-16 rounded-full bg-amber/10 mx-auto mb-4 flex items-center justify-center">
                    <Icons.Play />
                  </div>
                  <p className="text-sm text-cream-muted">视频生成后可预览</p>
                </div>
              )}
            </div>
          )}

          {activeTab === 'prompts' && (
            <div className="space-y-4">
              {promptTypes.map(({ key, label, value }) => (
                <div key={key} className="bg-black/30 rounded-lg p-4">
                  <div className="flex items-center justify-between mb-3">
                    <label className="text-[10px] font-mono text-cream-muted uppercase tracking-wider">
                      {label}
                    </label>
                    <button className="btn-icon text-xs">
                      <Icons.Wand />
                    </button>
                  </div>
                  <textarea
                    value={value}
                    onChange={(e) => onUpdate({ [key]: e.target.value } as Partial<Shot>)}
                    className="input-field text-[12px]"
                    rows={4}
                  />
                </div>
              ))}
            </div>
          )}

          {activeTab === 'settings' && (
            <div className="space-y-4">
              <div className="bg-black/30 rounded-lg p-4">
                <label className="text-[10px] font-mono text-cream-muted uppercase tracking-wider mb-3 block">
                  生成参数
                </label>
                <div className="space-y-3">
                  <div>
                    <label className="text-xs text-cream-muted mb-1 block">分辨率</label>
                    <select className="input-field">
                      <option>1920 x 1080 (1080p)</option>
                      <option>2560 x 1440 (2K)</option>
                      <option>3840 x 2160 (4K)</option>
                    </select>
                  </div>
                  <div>
                    <label className="text-xs text-cream-muted mb-1 block">风格</label>
                    <select className="input-field">
                      <option>电影质感 (Cinematic)</option>
                      <option>写实风格 (Realistic)</option>
                      <option>动漫风格 (Anime)</option>
                    </select>
                  </div>
                  <div>
                    <label className="text-xs text-cream-muted mb-1 block">帧率</label>
                    <select className="input-field">
                      <option>24 fps (电影标准)</option>
                      <option>30 fps (流畅)</option>
                      <option>60 fps (超流畅)</option>
                    </select>
                  </div>
                </div>
              </div>

              <div className="bg-black/30 rounded-lg p-4">
                <label className="text-[10px] font-mono text-cream-muted uppercase tracking-wider mb-3 block">
                  历史记录
                </label>
                <div className="space-y-2 text-xs text-cream-muted">
                  <div className="flex justify-between py-2 border-b border-white/5">
                    <span>2024-01-15 14:32</span>
                    <span className="text-amber">生成完成</span>
                  </div>
                  <div className="flex justify-between py-2 border-b border-white/5">
                    <span>2024-01-15 14:28</span>
                    <span>生成中...</span>
                  </div>
                  <div className="flex justify-between py-2">
                    <span>2024-01-15 13:45</span>
                    <span className="text-status-rejected">生成失败</span>
                  </div>
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Footer Actions */}
        <div className="sticky bottom-0 bg-bg-surface border-t border-amber/10 p-4 flex gap-3">
          <button className="btn-primary flex-1 flex items-center justify-center gap-2">
            <Icons.Sparkle />
            AI优化提示词
          </button>
          <button className="btn-secondary px-4">取消</button>
        </div>
      </div>
    </>
  );
};

// Control Bar Component
const ControlBar: React.FC<{
  shots: Shot[];
  autoAlign: boolean;
  onToggleAutoAlign: () => void;
  filter: string;
  onFilterChange: (filter: string) => void;
  onGenerateAll: () => void;
  onGenerateVideo: () => void;
}> = ({ shots, autoAlign, onToggleAutoAlign, filter, onFilterChange, onGenerateAll, onGenerateVideo }) => {
  const [showFilterDropdown, setShowFilterDropdown] = useState(false);
  const pendingCount = shots.filter(s => s.status === 'pending' || s.status === 'review').length;
  const canGenerateVideo = shots.filter(s => s.status === 'approved').length > 0;

  return (
    <footer className="fixed bottom-0 left-0 right-0 z-50 glass border-t border-amber/10">
      <div className="flex items-center justify-between px-6 py-4">
        {/* Left: Stats */}
        <div className="flex items-center gap-6">
          <div className="text-sm text-cream-muted">
            <span className="text-amber font-mono font-bold">{shots.length}</span> 个镜头
          </div>
          <div className="h-6 w-px bg-white/10" />
          <div className="flex items-center gap-4 text-xs">
            <span className="text-cream-muted">
              <span className="text-status-pending font-mono">{shots.filter(s => s.status === 'pending').length}</span> 待生成
            </span>
            <span className="text-cream-muted">
              <span className="text-status-review font-mono">{shots.filter(s => s.status === 'review').length}</span> 待审核
            </span>
            <span className="text-cream-muted">
              <span className="text-status-approved font-mono">{shots.filter(s => s.status === 'approved').length}</span> 通过
            </span>
          </div>
        </div>

        {/* Center: Auto Align Toggle */}
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2">
            <span className="text-sm text-cream-muted">前后帧对齐</span>
            <button
              onClick={onToggleAutoAlign}
              className={`toggle-switch ${autoAlign ? 'active' : ''}`}
            />
          </div>

          <div className="h-6 w-px bg-white/10" />

          {/* Filter Dropdown */}
          <div className="dropdown relative">
            <button
              onClick={() => setShowFilterDropdown(!showFilterDropdown)}
              className="flex items-center gap-2 text-sm text-cream-muted hover:text-cream transition-colors"
            >
              状态筛选: <span className="text-cream">{filter}</span>
              <Icons.ChevronDown />
            </button>
            {showFilterDropdown && (
              <div className="dropdown-menu">
                {statusFilters.map(f => (
                  <div
                    key={f}
                    onClick={() => {
                      onFilterChange(f);
                      setShowFilterDropdown(false);
                    }}
                    className={`dropdown-item ${filter === f ? 'text-amber bg-amber/10' : ''}`}
                  >
                    {f}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Right: Action Buttons */}
        <div className="flex items-center gap-3">
          <button
            onClick={onGenerateVideo}
            disabled={!canGenerateVideo}
            className={`btn-secondary flex items-center gap-2 ${
              !canGenerateVideo ? 'opacity-50 cursor-not-allowed' : ''
            }`}
          >
            <Icons.Play />
            生成视频
          </button>
          <button
            onClick={onGenerateAll}
            disabled={pendingCount === 0}
            className={`btn-primary pulsing flex items-center gap-2 ${
              pendingCount === 0 ? 'opacity-50 cursor-not-allowed' : ''
            }`}
          >
            <Icons.Sparkle />
            一键生成所有帧
            {pendingCount > 0 && (
              <span className="bg-black/30 px-2 py-0.5 rounded-full text-xs font-mono">
                {pendingCount}
              </span>
            )}
          </button>
        </div>
      </div>
    </footer>
  );
};

// Main App Component
const App: React.FC = () => {
  const [shots, setShots] = useState<Shot[]>(initialShots);
  const [currentStep, setCurrentStep] = useState(3); // 审核&生成视频
  const [selectedShot, setSelectedShot] = useState<Shot | null>(null);
  const [autoAlign, setAutoAlign] = useState(true);
  const [filter, setFilter] = useState('全部');
  const [isLoaded, setIsLoaded] = useState(false);

  useEffect(() => {
    // Cinematic reveal animation
    const timer = setTimeout(() => setIsLoaded(true), 100);
    return () => clearTimeout(timer);
  }, []);

  const updateShot = (id: number, updated: Partial<Shot>) => {
    setShots(prev => prev.map(s => s.id === id ? { ...s, ...updated } : s));
    if (selectedShot?.id === id) {
      setSelectedShot(prev => prev ? { ...prev, ...updated } : null);
    }
  };

  const filteredShots = filter === '全部'
    ? shots
    : shots.filter(s => statusLabels[s.status] === filter);

  const handleGenerateAll = () => {
    const pendingShots = shots.filter(s => s.status === 'pending');
    pendingShots.forEach((shot, index) => {
      setTimeout(() => {
        updateShot(shot.id, { status: 'generating' });
        setTimeout(() => {
          updateShot(shot.id, {
            status: 'review',
            keyframeImage: { url: `https://picsum.photos/seed/shot${shot.id}kf${Date.now()}/400/225`, timestamp: new Date() },
            firstFrameImage: { url: `https://picsum.photos/seed/shot${shot.id}ff${Date.now()}/400/225`, timestamp: new Date() },
            lastFrameImage: { url: `https://picsum.photos/seed/shot${shot.id}lf${Date.now()}/400/225`, timestamp: new Date() },
          });
        }, 2500);
      }, index * 500);
    });
  };

  const handleGenerateVideo = () => {
    const approvedShots = shots.filter(s => s.status === 'approved');
    console.log('Generating video with approved shots:', approvedShots.length);
    // Simulate video generation
  };

  const addNewShot = () => {
    const newId = Math.max(...shots.map(s => s.id)) + 1;
    const newShot: Shot = {
      id: newId,
      status: 'pending',
      sceneDescription: '',
      referenceImage: null,
      keyframePrompt: '',
      firstFramePrompt: '',
      lastFramePrompt: '',
      keyframeImage: null,
      firstFrameImage: null,
      lastFrameImage: null,
      videoUrl: null,
      satisfaction: null,
    };
    setShots(prev => [...prev, newShot]);
  };

  return (
    <div className={`min-h-screen bg-bg-base ${isLoaded ? 'animate-fade-in' : 'opacity-0'}`}>
      {/* Film Grain Overlay */}
      <div className="film-grain" />
      
      {/* Scan Lines */}
      <div className="scan-lines" />
      
      {/* Letterbox Elements */}
      <div className="letterbox-top hidden lg:block" />
      <div className="letterbox-bottom hidden lg:block" />

      {/* Grid Background */}
      <div className="fixed inset-0 grid-bg pointer-events-none" />

      {/* Top Navigation */}
      <TopNav currentStep={currentStep} onStepChange={setCurrentStep} />

      {/* Main Content */}
      <main className="pt-24 pb-32 px-6 min-h-screen">
        {/* Storyboard Canvas */}
        <div className="flex items-start gap-6 overflow-x-auto pb-6 pt-4">
          {filteredShots.map((shot, index) => (
            <div key={shot.id} className="flex-shrink-0" style={{ width: '340px' }}>
              <ShotCard
                shot={shot}
                index={index}
                onSelect={() => setSelectedShot(shot)}
                onUpdate={(updated) => updateShot(shot.id, updated)}
              />
            </div>
          ))}

          {/* Add Shot Button */}
          <button
            onClick={addNewShot}
            className="flex-shrink-0 w-[340px] h-[500px] glass rounded-xl border-2 border-dashed border-amber/20 flex flex-col items-center justify-center gap-4 hover:bg-amber/5 hover:border-amber/40 transition-all duration-300 group"
          >
            <div className="w-16 h-16 rounded-full bg-amber/10 flex items-center justify-center group-hover:bg-amber/20 transition-colors">
              <Icons.Plus />
            </div>
            <span className="text-cream-muted group-hover:text-amber transition-colors font-medium">
              添加新镜头
            </span>
          </button>
        </div>
      </main>

      {/* Control Bar */}
      <ControlBar
        shots={shots}
        autoAlign={autoAlign}
        onToggleAutoAlign={() => setAutoAlign(!autoAlign)}
        filter={filter}
        onFilterChange={setFilter}
        onGenerateAll={handleGenerateAll}
        onGenerateVideo={handleGenerateVideo}
      />

      {/* Detail Panel Drawer */}
      {selectedShot && (
        <DetailPanel
          shot={selectedShot}
          onClose={() => setSelectedShot(null)}
          onUpdate={(updated) => updateShot(selectedShot.id, updated)}
        />
      )}
    </div>
  );
};

export default App;