import type { ReactNode } from "react";

/** Discord embed markup (**bold**, `code`, <t:epoch:F>) as readable text with emphasis kept. */
export function DiscordText({ text }: { text: string }) {
  const parts: ReactNode[] = [];
  const source = text.replace(/<t:(\d+)(?::[a-zA-Z])?>/g, (_match, epoch: string) => new Date(Number(epoch) * 1000).toLocaleString("ko-KR")).replace(/```\w*\n?/g, "");
  source.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).forEach((piece, index) => {
    if (piece.startsWith("**") && piece.endsWith("**")) parts.push(<b key={index}>{piece.slice(2, -2)}</b>);
    else if (piece.startsWith("`") && piece.endsWith("`")) parts.push(<code key={index}>{piece.slice(1, -1)}</code>);
    else if (piece) parts.push(piece);
  });
  return <span className="discord-text">{parts}</span>;
}

export type Embed = { title: string; description: string; fields: { name: string; value: string; inline: boolean }[]; footer: string };

export function EmbedView({ embed }: { embed: Embed }) {
  return <article className="embed-view">
    {embed.description && <p><DiscordText text={embed.description} /></p>}
    {embed.fields.length > 0 && <div className="embed-fields">{embed.fields.map((field, index) => <section key={index} className={field.inline ? "inline" : undefined}><h4><DiscordText text={field.name} /></h4><p><DiscordText text={field.value} /></p></section>)}</div>}
    {embed.footer && <small>{embed.footer}</small>}
  </article>;
}
