"""Run a dedicated test application with separate durable state and command scope."""
import argparse
import os
from pathlib import Path
import runpy
import struct
import sys
import sysconfig

ROOT = Path(__file__).resolve().parents[1]


def positive_id(text):
    value=int(text)
    if value<=0:
        raise argparse.ArgumentTypeError('Use a positive Discord ID.')
    return value


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--guild',required=True,type=positive_id)
    parser.add_argument('--overseer-role',required=True,type=positive_id)
    parser.add_argument('--playing-role',required=True,type=positive_id)
    parser.add_argument('--env-file',type=Path,default=ROOT/'.env.test',help='Local settings for a dedicated test application; never print its token.')
    parser.add_argument('--check',action='store_true',help='Check the isolated profile without connecting to Discord.')
    args=parser.parse_args()
    if struct.calcsize('P')!=8 or sysconfig.get_config_var('Py_GIL_DISABLED'):
        parser.error('Use standard 64-bit CPython, with the GIL enabled.')
    state_dir=ROOT/'state'/f'discord-test-{args.guild}'
    print(f'Runtime: Python {sys.version.split()[0]}; test guild: {args.guild}; state: {state_dir}')
    if args.check:
        return
    from dotenv import dotenv_values
    if not args.env_file.is_file():
        parser.error('Create the ignored .env.test file with the dedicated test application token first.')
    values=dotenv_values(args.env_file)
    token=values.get('DISCORD_TOKEN') or values.get('DISCORD_BOT_TOKEN')
    if not token:
        parser.error('The test settings file must contain a dedicated test application token.')
    # Inherited deployment settings must not select production guilds, roles or private channels.
    os.environ.update({k:str(v) for k,v in values.items() if v is not None})
    os.environ.update(DISCORD_TOKEN=token,ALLOWED_GUILD_ID=str(args.guild),
        GAME_OVERSEER_ROLE_ID=str(args.overseer_role),PLAYING_ROLE_ID=str(args.playing_role),GAME_CATEGORY_ID='0',
        PLAYER_PRIVATE_CHANNEL_IDS=values.get('PLAYER_PRIVATE_CHANNEL_IDS') or '{}',
        MAFIABOT_TEST_PROFILE='1',MAFIABOT_INSTANCE_LOCK_PATH=str(state_dir/'bot.instance.lock'),MAFIABOT_ALLOW_MULTI='0')
    sys.path.insert(0,str(ROOT))
    import persistence
    persistence.STATE_DIR=state_dir
    runpy.run_path(str(ROOT/'bot.py'),run_name='__main__')


if __name__=='__main__':
    main()
