/**
 * chainAnnounce — the pane's half of Remote Crew chaining.
 *
 * When this dashboard runs inside an InstancesViewport <iframe>, the gateway it
 * belongs to is itself a crew of the gateway showing the page. A crew connected
 * from in here is therefore reachable from the host only by riding the host's
 * existing hop to us, and the host cannot learn about it on its own: it never
 * sees our registry.
 *
 * So the pane says so. One postMessage upward, carrying WHICH crew came up and
 * on WHICH of our loopback ports — and nothing else. In particular it carries no
 * token and no credential: the host mints its own by asking our gateway over the
 * credential it already holds for us (`/api/instances/{id}/embed-token`), which
 * is what makes it safe for this notice to travel through frame code at all.
 *
 * The host decides what to do with it. It validates the sender's origin against
 * its own warm-pane map, then asks ITS gateway to add the crew, where the depth
 * cap and the cycle guard live. Nothing here is trusted, and nothing here is a
 * decision.
 *
 * A no-op at top level: a gateway nobody is embedding has no host to tell.
 */
import { isEmbeddedPane } from './embedded'

/** What the pane tells its host about a crew it just connected. */
export interface ChainedCrewNotice {
  /** The crew's id in OUR registry. The host derives its own id; this only
   *  labels the notice and lets the host recognise a re-announce. */
  id: string
  /** The crew's display name, as the user typed it here. */
  name: string
  /** The host/target string that names the machine on a switcher row. */
  sshHost: string
  /** The port the crew's OWN gateway listens on, for the host's record. */
  remotePort: number
  /** The loopback port on THIS machine where our forward to the crew listens.
   *  This is the far end of the forward the host will open. */
  port: number
}

export const CHAINED_CREW_MESSAGE = 'mc-instance-ready'

/**
 * Tell the host a crew is connected here and reachable through us.
 *
 * Silent on every failure. A pane with no host, a host that predates the
 * message, and a browser that refuses the post all mean the same thing to the
 * user: the crew is connected HERE and simply does not appear as a tab up there.
 * Throwing would instead break the connect that just succeeded.
 */
export function announceChainedCrew(notice: ChainedCrewNotice): boolean {
  if (!isEmbeddedPane()) return false
  if (!notice.id || !Number.isInteger(notice.port) || notice.port < 1 || notice.port > 65535) {
    return false
  }
  try {
    // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
    window.parent?.postMessage(
      {
        type: CHAINED_CREW_MESSAGE,
        v: 1,
        id: notice.id,
        name: notice.name,
        sshHost: notice.sshHost,
        remotePort: notice.remotePort,
        port: notice.port,
      },
      // The host validates our ORIGIN against the loopback port it forwarded to
      // us, which is the check that matters; we cannot name its origin from here
      // (a cross-origin iframe cannot read `parent.location`), and the payload
      // carries nothing secret precisely so this is safe.
      '*',
    )
    return true
  } catch {
    return false
  }
}
