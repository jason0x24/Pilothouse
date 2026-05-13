import { api } from "@/lib/api";

/** Server component — renders the tenant pill in the nav.
 *
 * If the API is unreachable or rejects (no key configured) we render a
 * neutral "anonymous" pill rather than failing the entire layout. The
 * console is supposed to be useful even when the API is briefly down. */
export async function TenantBadge() {
  try {
    const me = await api.me();
    return (
      <span className="pill pill-info" title={`tenant id: ${me.tenant_id}`}>
        tenant: {me.tenant_display_name || me.tenant_name}
      </span>
    );
  } catch {
    return <span className="pill pill-neutral">tenant: anonymous</span>;
  }
}
