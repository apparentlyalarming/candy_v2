-- ============================================================================
-- candy_v2 — seed reply catalog (ported from the original candy bot)
-- ============================================================================
-- Run AFTER schema.sql. Idempotent: upserts on `key`, so re-running replaces
-- the default templates without touching guild overrides in guild_replies.
--
-- These are the ORIGINAL replies from the v1 bot, converted to the
-- {user}/{target} placeholder format. Raw <@id> mentions are never stored.
--
-- WHY A SEPARATE FILE: schema.sql defines structure; this file defines content.
-- You can edit or extend this without touching the schema, and a server that
-- has customised replies keeps its overrides (guild_replies is untouched).
-- ============================================================================


-- ============================================================================
-- ACTIONS
-- ============================================================================

-- 1) HUG — 30 replies, ported verbatim from v1 (tight / gentle / sisterly)
insert into public.reply_actions (key, label, emoji, sort_order, default_templates)
values (
    'hug', 'Hug', '🫂', 10,
    array[
        -- tight & clingy
        '{user} wraps her arms tightly around {target} and refuses to let go. 🫂',
        '{user} completely engulfs {target} in a warm, cozy embrace.',
        '{user} pulls {target} in close and rests her chin on their shoulder.',
        '{user} traps {target} in a literal blanket of affection.',
        '{user} gently squeezes {target} and just holds them there for a moment.',
        '{user} buries her face into {target}''s shoulder with a happy little sigh.',
        '{user} sneaks up behind {target} and wraps her arms around their waist.',
        '{user} pulls {target} into a sudden, tight squeeze that smells like sugar.',
        '{user} latches onto {target} like a very affectionate koala. 🐨',
        '{user} wraps {target} up like a human burrito and won''t let them escape.',
        -- gentle & comforting
        '{user} gives {target} a gentle squeeze that just feels like home. ✨',
        '{user} wraps one arm around {target}''s shoulders and pulls them close.',
        '{user} rests her head against {target}''s arm comfortably.',
        '{user} softly wraps her arms around {target} so they feel safe.',
        '{user} gives {target} a firm, reassuring squeeze. ''i got you.''',
        '{user} playfully bumps her shoulder against {target}''s and pulls them in.',
        '{user} holds onto {target} tightly and sways them side to side.',
        '{user} gives {target} a quick, warm squeeze before letting go.',
        '{user} gently pulls {target} in by the shoulders for a quiet moment.',
        '{user} just holds {target} for a minute because they looked like they needed it.',
        -- sisterly & cool
        '{user} pulls {target} into a protective grip. ''nobody messes with you.''',
        '{user} gives {target} an awkward side-squeeze. ''vibes are immaculate.'' 😎',
        '{user} ruffles {target}''s hair while keeping them in a friendly headlock.',
        '{user} wraps {target} in an embrace and shares some of that ''cool sis'' energy.',
        '{user} gives {target} a playful shove, then immediately pulls them into a tight squeeze.',
        '{user} slings an arm heavily around {target}''s neck and grins.',
        '{user} holds {target} close and whispers ''don''t tell Rosie, but you''re cool too.''',
        '{user} pulls {target} into an embrace that makes them feel like part of the family.',
        '{user} gives {target} a supportive squeeze that hypes them up! 🔥',
        '{user} wraps {target} in an embrace and shares a little bit of her candy-sweet warmth.'
    ]
)
on conflict (key) do update
    set label = excluded.label,
        emoji = excluded.emoji,
        sort_order = excluded.sort_order,
        default_templates = excluded.default_templates;


-- 2) PUNCH — ported from v1
insert into public.reply_actions (key, label, emoji, sort_order, default_templates)
values (
    'punch', 'Punch', '🥊', 20,
    array[
        '{user} winds up and lands a solid jab right on {target}''s shoulder.',
        '{user} playfully socks {target} in the arm. ''tag, you''re it.''',
        '{user} delivers a swift uppercut to {target}, sending them flying into the stratosphere! 🚀',
        '{user} lightly taps their knuckles against {target}''s arm in a friendly greeting.',
        '{user} swings at {target} but completely misses and spins around in a circle. 💫',
        '{user} gives {target} a quick one-two combo! sporty and efficient. 🥊',
        '{user} bops {target} right on the nose. boop.',
        '{user} throws a right hook that catches {target} completely off guard.',
        '{user} knocks {target} straight into a wall with a massive strike. ouch.',
        '{user} fist-bumps {target}''s face with a little too much enthusiasm.'
    ]
)
on conflict (key) do update
    set label = excluded.label,
        emoji = excluded.emoji,
        sort_order = excluded.sort_order,
        default_templates = excluded.default_templates;


-- 3) KICK — ported from v1
insert into public.reply_actions (key, label, emoji, sort_order, default_templates)
values (
    'kick', 'Kick', '🦵', 30,
    array[
        '{user} delivers a flawless roundhouse to {target}''s face! 🥋',
        '{user} sneakily sweeps {target}''s legs out from under them. timber!',
        '{user} boots {target} clear across the server like a soccer ball. ⚽',
        '{user} playfully nudges {target}''s shin with their foot.',
        '{user} goes for a high-kick but accidentally pulls a muscle. {target} wins this round.',
        '{user} dropkicks {target} through a theoretical window. crash!',
        '{user} lightly taps {target}''s ankle to get their attention.',
        '{user} sparta-boots {target} right into a bottomless pit. ''THIS IS CANDY!''',
        '{user} attempts a backflip strike on {target} and actually lands it perfectly. 10/10.',
        '{user} kicks a pile of dust into {target}''s eyes and runs away laughing.'
    ]
)
on conflict (key) do update
    set label = excluded.label,
        emoji = excluded.emoji,
        sort_order = excluded.sort_order,
        default_templates = excluded.default_templates;


-- 4) SLAP — ported from v1
insert into public.reply_actions (key, label, emoji, sort_order, default_templates)
values (
    'slap', 'Slap', '👋', 40,
    array[
        '{user} dramatically strikes {target} across the face like they''re in a soap opera. 📺',
        '{user} gives {target} a quick smack on the back of the head. ''wake up!''',
        '{user} high-fives {target}''s face with extreme prejudice.',
        '{user} winds up their hand and leaves a bright red mark on {target}''s cheek. 👋',
        '{user} lightly taps {target}''s cheek. ''snap out of it.''',
        '{user} swings at {target} but hits themselves in the face instead. oops.',
        '{user} delivers a swift smack to {target} using a wet noodle. thwap!',
        '{user} backhands {target} all the way into the next dimension. 🌌',
        '{user} claps their hands against both of {target}''s ears. ringing for days.',
        '{user} gives {target} a playful swat to keep them in line.'
    ]
)
on conflict (key) do update
    set label = excluded.label,
        emoji = excluded.emoji,
        sort_order = excluded.sort_order,
        default_templates = excluded.default_templates;


-- 5) DANCE — v1 took no target. Kept targetless-compatible by using {target}
--    only in a few lines; the slash command still accepts an optional user.
insert into public.reply_actions (key, label, emoji, sort_order, default_templates)
values (
    'dance', 'Dance', '💃', 50,
    array[
        '{user} busts out the dougie and absolutely nails it. 💃',
        '{user} hits the dance floor and drags {target} along for the ride.',
        '{user} does a little spin and strikes a pose. flawless.',
        '{user} is dancing like nobody is watching. (everyone is watching.)',
        '{user} challenges {target} to a dance-off and wins on style alone.',
        '{user} shuffles across the floor with maximum confidence.'
    ]
)
on conflict (key) do update
    set label = excluded.label,
        emoji = excluded.emoji,
        sort_order = excluded.sort_order,
        default_templates = excluded.default_templates;


-- 6) PAT
insert into public.reply_actions (key, label, emoji, sort_order, default_templates)
values (
    'pat', 'Pat', '🫳', 60,
    array[
        '{user} pats {target} on the head',
        '{user} gives {target} gentle headpats',
        '{user} pats {target} softly, twice',
        '{user} showers {target} with headpats',
        '{user} pats {target} and tells them it''ll be okay.'
    ]
)
on conflict (key) do update
    set label = excluded.label,
        emoji = excluded.emoji,
        sort_order = excluded.sort_order,
        default_templates = excluded.default_templates;


-- 7) POKE
insert into public.reply_actions (key, label, emoji, sort_order, default_templates)
values (
    'poke', 'Poke', '👉', 70,
    array[
        '{user} pokes {target}',
        '{user} pokes {target} right in the cheek',
        '{user} pokes {target} repeatedly until they respond',
        '{user} pokes {target} with zero remorse'
    ]
)
on conflict (key) do update
    set label = excluded.label,
        emoji = excluded.emoji,
        sort_order = excluded.sort_order,
        default_templates = excluded.default_templates;


-- 8) CUDDLE
insert into public.reply_actions (key, label, emoji, sort_order, default_templates)
values (
    'cuddle', 'Cuddle', '🫂', 80,
    array[
        '{user} cuddles up to {target}',
        '{user} and {target} are now a cuddle puddle',
        '{user} pulls {target} into a cozy cuddle',
        '{user} cuddles {target} until they fall asleep'
    ]
)
on conflict (key) do update
    set label = excluded.label,
        emoji = excluded.emoji,
        sort_order = excluded.sort_order,
        default_templates = excluded.default_templates;


-- 9) BOOP
insert into public.reply_actions (key, label, emoji, sort_order, default_templates)
values (
    'boop', 'Boop', '👆', 90,
    array[
        '{user} boops {target} on the nose',
        '{user} gives {target} a tiny boop',
        '{user} boops {target}. boop!',
        '{user} sneaks up and boops {target}'
    ]
)
on conflict (key) do update
    set label = excluded.label,
        emoji = excluded.emoji,
        sort_order = excluded.sort_order,
        default_templates = excluded.default_templates;


-- 10) HIGHFIVE
insert into public.reply_actions (key, label, emoji, sort_order, default_templates)
values (
    'highfive', 'High Five', '🖐️', 100,
    array[
        '{user} high-fives {target}!',
        '{user} and {target} slap hands in perfect sync',
        '{user} goes for the high five and {target} actually catches it',
        '{user} high-fives {target} so hard it echoes'
    ]
)
on conflict (key) do update
    set label = excluded.label,
        emoji = excluded.emoji,
        sort_order = excluded.sort_order,
        default_templates = excluded.default_templates;


-- 11) WAVE
insert into public.reply_actions (key, label, emoji, sort_order, default_templates)
values (
    'wave', 'Wave', '👋', 110,
    array[
        '{user} waves at {target}',
        '{user} waves enthusiastically at {target}',
        '{user} gives {target} a little wave',
        '{user} waves at {target} from across the room'
    ]
)
on conflict (key) do update
    set label = excluded.label,
        emoji = excluded.emoji,
        sort_order = excluded.sort_order,
        default_templates = excluded.default_templates;


-- 12) BITE
insert into public.reply_actions (key, label, emoji, sort_order, default_templates)
values (
    'bite', 'Bite', '😬', 120,
    array[
        '{user} bites {target}',
        '{user} takes a small nibble out of {target}',
        '{user} chomps {target} harmlessly',
        '{user} bites {target} - purely a crime of passion'
    ]
)
on conflict (key) do update
    set label = excluded.label,
        emoji = excluded.emoji,
        sort_order = excluded.sort_order,
        default_templates = excluded.default_templates;


-- 13) BONK
insert into public.reply_actions (key, label, emoji, sort_order, default_templates)
values (
    'bonk', 'Bonk', '🔨', 130,
    array[
        '{user} bonks {target} on the head',
        '{user} sends {target} to horny jail with a bonk',
        '{user} bonks {target}. go to jail.',
        '{user} applies a firm bonk to {target}'
    ]
)
on conflict (key) do update
    set label = excluded.label,
        emoji = excluded.emoji,
        sort_order = excluded.sort_order,
        default_templates = excluded.default_templates;


-- ============================================================================
-- VERIFY
-- ============================================================================
-- select key, label, array_length(default_templates, 1) as replies
--   from public.reply_actions
--  order by sort_order;

-- ============================================================================
-- END OF SEED
-- ============================================================================
