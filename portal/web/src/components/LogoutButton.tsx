"use client";

interface LogoutButtonProps {
  className?: string;
}

/**
 * POST /api/logout in a plain form — HttpOnly session cookie; the handler
 * is a no-op without a session. Kept as a native form so no JS is required
 * for the security-critical action (app.js behavior preserved).
 */
export default function LogoutButton({ className }: LogoutButtonProps) {
  return (
    <form action="/api/logout" method="post" noValidate>
      <button type="submit" className={`btn-ghost btn-logout ${className ?? ""}`}>
        Abmelden
      </button>
    </form>
  );
}
