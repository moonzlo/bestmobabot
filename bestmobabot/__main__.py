import json
import signal
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, TextIO, Tuple

import click
import coloredlogs

from bestmobabot.api import API
from bestmobabot.bot import Bot
from bestmobabot.logger import logger


@click.command()
@click.option('-s', '--remixsid', help='VK.com remixsid cookie.', envvar='BESTMOBABOT_REMIXSID', required=True)
@click.option('--no-experience', help='Do not farm experience.', envvar='BESTMOBABOT_NO_EXPERIENCE', is_flag=True)
@click.option('raids', '--raid', help='Raid the mission specified by its ID and number of raids per day.', envvar='BESTMOBABOT_RAID', type=(str, int), multiple=True)
@click.option('--battle-log', help='Log battles results into JSON Lines file.', envvar='BESTMOBABOT_BATTLE_LOG', type=click.File('at'))
@click.option('-v', '--verbose', help='Increase verbosity.', is_flag=True)
@click.option('-l', '--log-file', help='Log file.', envvar='BESTMOBABOT_LOGFILE', type=click.File('at'), default=click.get_text_stream('stderr'))
@click.option('shops', '--shop', help='Buy goods specified by shop_id and slot_id every day', envvar='BESTMOBABOT_SHOP', type=(str, str), multiple=True)
def main(
    remixsid: str,
    no_experience: bool,
    raids: Tuple[Tuple[str, int], ...],
    battle_log: Optional[TextIO],
    verbose: bool,
    log_file: TextIO,
    shops: Tuple[Tuple[str, str], ...],
):
    """
    Hero Wars bot.
    """
    signal.signal(signal.SIGTERM, handle_sigterm)
    level = 'DEBUG' if verbose else 'INFO'
    coloredlogs.install(fmt='%(asctime)s %(levelname)s %(message)s', level=level, logger=logger, stream=log_file)
    logger.info('🤖 Bot is starting.')

    with API(remixsid) as api:
        # Try to read cached state.
        state_path = Path(f'remixsid-{remixsid}.json')
        state = read_state(state_path)
        # Start the bot.
        api.start(state)
        with Bot(api, no_experience, list(raids), list(shops), battle_log) as bot:
            bot.start(state)
            logger.info(f'👋 Welcome {bot.user.name}! Your game time is {datetime.now(bot.user.tz):%H:%M:%S}.')
            logger.info('👋 Next day starts at %s.', bot.user.next_day)
            try:
                bot.run()
            except KeyboardInterrupt:
                # Save the state for faster restart.
                logger.info('🔑 Writing state to %s…', state_path)
                state_path.write_text(json.dumps({
                    'datetime': datetime.now().timestamp(),
                    **api.state,
                    **bot.state,
                }, indent=2, ensure_ascii=False))
                raise


def read_state(path: Path) -> Optional[Dict]:
    if not path.is_file():
        logger.info('😐 No saved state found.')
        return None
    logger.info('😀 Reading saved state from %s…', path)
    state = json.loads(path.read_text())
    if datetime.now() - datetime.fromtimestamp(state['datetime']) > timedelta(days=1):
        logger.warning('😐 Saved state is too old.')
        return None
    return state


# noinspection PyUnusedLocal
def handle_sigterm(signum, frame):
    raise KeyboardInterrupt


if __name__ == '__main__':
    main()
