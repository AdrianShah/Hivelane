import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import './index.css'
import LegacyRoomRedirect from './pages/LegacyRoomRedirect.tsx'
import RoomRoute from './pages/RoomRoute.tsx'
import Start from './pages/Start.tsx'
import About from './site/About.tsx'
import Landing from './site/Landing.tsx'
import { Privacy, Security, Terms } from './site/Legal.tsx'
import Updates from './site/Updates.tsx'
import { basePath } from './relayUrl'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter basename={basePath() || '/'}>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/start" element={<Start />} />
        <Route path="/about" element={<About />} />
        <Route path="/updates" element={<Updates />} />
        <Route path="/blog" element={<Navigate to="/updates?type=post" replace />} />
        <Route path="/changelog" element={<Navigate to="/updates?type=changelog" replace />} />
        <Route path="/privacy" element={<Privacy />} />
        <Route path="/terms" element={<Terms />} />
        <Route path="/security" element={<Security />} />
        <Route path="/r/:code" element={<RoomRoute />} />
        <Route path="/room/:code" element={<LegacyRoomRedirect />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
)
