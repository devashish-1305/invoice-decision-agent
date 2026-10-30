import { NavLink, Route, Routes } from "react-router-dom";
import Dashboard from "./pages/Dashboard";
import LiveRun from "./pages/LiveRun";
import NewRun from "./pages/NewRun";
import Reference from "./pages/Reference";

const NAV = [
  { to: "/", label: "New run", end: true },
  { to: "/dashboard", label: "Dashboard", end: false },
  { to: "/reference", label: "Reference data", end: false },
];

export default function App() {
  return (
    <div className="flex min-h-full">
      <aside className="sticky top-0 h-screen w-[232px] shrink-0 border-r border-line bg-white px-3.5 py-5">
        <div className="flex items-center gap-2.5 px-1">
          <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-accent text-[15px] font-semibold text-white" aria-hidden>I</span>
          <div>
            <p className="text-[14px] font-semibold leading-tight text-ink">Invoice Decision Agent</p>
            <p className="text-[12px] text-muted">Northwind Retail · <span className="text-accent">Demo</span></p>
          </div>
        </div>
        <nav className="mt-6 space-y-1" aria-label="Main">
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.end}
              className={({ isActive }) => `block rounded-lg px-2.5 py-[9px] text-[14px] ${isActive ? "bg-accent-bg font-medium text-accent-text" : "text-body hover:bg-page"}`}>
              {n.label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <main className="min-w-0 flex-1 px-10 py-8">
        <div className="mx-auto max-w-content">
          <Routes>
            <Route path="/" element={<NewRun />} />
            <Route path="/runs/:id" element={<LiveRun />} />
            <Route path="/dashboard" element={<Dashboard />} />
            <Route path="/reference" element={<Reference />} />
            <Route path="*" element={<NewRun />} />
          </Routes>
        </div>
      </main>
    </div>
  );
}
