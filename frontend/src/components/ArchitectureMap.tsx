/** Decorative architecture visualization for agent lanes. */
export default function ArchitectureMap() {
  return (
    <div className="relative rounded-xl overflow-hidden border border-outline-soft/90 bg-[#060a12] h-[116px]">
      <div
        className="absolute inset-0 opacity-40"
        style={{
          backgroundImage:
            'radial-gradient(circle at 20% 40%, rgba(94,234,212,0.15), transparent 40%), radial-gradient(circle at 80% 30%, rgba(165,180,252,0.18), transparent 35%)',
        }}
      />
      <svg
        viewBox="0 0 400 120"
        className="absolute inset-0 w-full h-full"
        aria-hidden
      >
        <defs>
          <radialGradient id="nodeGlow" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="#5EEAD4" stopOpacity="0.85" />
            <stop offset="100%" stopColor="#5EEAD4" stopOpacity="0" />
          </radialGradient>
          <linearGradient id="edgeGrad" x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" stopColor="#818CF8" stopOpacity="0.15" />
            <stop offset="50%" stopColor="#5EEAD4" stopOpacity="0.65" />
            <stop offset="100%" stopColor="#A5B4FC" stopOpacity="0.15" />
          </linearGradient>
        </defs>

        {Array.from({ length: 8 }).map((_, i) => (
          <line
            key={`h-${i}`}
            x1="0"
            y1={i * 16}
            x2="400"
            y2={i * 16}
            stroke="#1e293b"
            strokeWidth="0.5"
            strokeOpacity="0.7"
          />
        ))}
        {Array.from({ length: 14 }).map((_, i) => (
          <line
            key={`v-${i}`}
            x1={i * 30}
            y1="0"
            x2={i * 30}
            y2="120"
            stroke="#1e293b"
            strokeWidth="0.5"
            strokeOpacity="0.7"
          />
        ))}

        <path
          d="M60 70 L140 40 L220 65 L300 35 L360 55"
          fill="none"
          stroke="url(#edgeGrad)"
          strokeWidth="1.5"
        />
        <path
          d="M80 90 L160 75 L240 95 L320 70"
          fill="none"
          stroke="#818CF8"
          strokeOpacity="0.4"
          strokeWidth="1"
          strokeDasharray="3 4"
        />

        {[
          [60, 70],
          [140, 40],
          [220, 65],
          [300, 35],
          [360, 55],
          [160, 75],
          [240, 95],
        ].map(([x, y], i) => (
          <g key={i}>
            <circle cx={x} cy={y} r="9" fill="url(#nodeGlow)" opacity="0.3" />
            <circle
              cx={x}
              cy={y}
              r="3"
              fill={i % 2 === 0 ? '#5EEAD4' : '#A5B4FC'}
            />
          </g>
        ))}
      </svg>
      <div className="absolute bottom-2.5 left-3.5 flex items-center gap-1.5">
        <span className="material-symbols-outlined text-primary text-[15px]">hub</span>
        <span className="section-label">Architecture Map</span>
      </div>
    </div>
  );
}
