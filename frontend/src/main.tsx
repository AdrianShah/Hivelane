import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import './index.css'
import Home from './pages/Home.tsx'
import LegacyRoomRedirect from './pages/LegacyRoomRedirect.tsx'
import RoomRoute from './pages/RoomRoute.tsx'
import { basePath } from './relayUrl'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter basename={basePath() || '/'}>
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/r/:code" element={<RoomRoute />} />
        <Route path="/room/:code" element={<LegacyRoomRedirect />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
)
