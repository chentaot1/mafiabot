"""Bound Discord output independently of the application entrypoint."""
import discord


def chunk_lines(lines, *, max_chars=1024):
    chunks, current = [], ''
    for raw in lines:
        line = str(raw)
        pieces = [line[i:i + max_chars] for i in range(0, len(line), max_chars)] or ['']
        for piece in pieces:
            joined = current + ('\n' if current else '') + piece
            if len(joined) > max_chars:
                chunks.append(current)
                current = piece
            else:
                current = joined
    if current:
        chunks.append(current)
    return chunks or ['(none)']


def section_embeds(title, sections, *, color=None):
    embeds = []
    embed = discord.Embed(title=title[:256], color=color)
    for name, lines in sections:
        for index, chunk in enumerate(chunk_lines(lines)):
            field = name if index == 0 else name + ' (continued)'
            if len(embed.fields) >= 25 or len(embed) + len(field) + len(chunk) > 5500:
                embeds.append(embed)
                embed = discord.Embed(title=title[:256], color=color)
            embed.add_field(name=field[:256], value=chunk, inline=False)
    embeds.append(embed)
    return embeds
