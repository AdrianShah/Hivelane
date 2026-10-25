/**
 * Copy text to the clipboard. Falls back to a hidden textarea where the async
 * clipboard API is unavailable, a dashboard opened over plain http on a LAN
 * address is not a secure context, and that is exactly where people share
 * room codes from.
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const el = document.createElement('textarea');
    el.value = text;
    el.setAttribute('readonly', '');
    el.style.position = 'fixed';
    el.style.opacity = '0';
    document.body.appendChild(el);
    el.select();
    let copied = false;
    try {
      copied = document.execCommand('copy');
    } catch {
      copied = false;
    }
    document.body.removeChild(el);
    return copied;
  }
}
