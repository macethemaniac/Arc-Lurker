import discord
import os
import tweepy
import asyncio
import re
import requests
import logging
from discord.ext import commands, tasks
from datetime import datetime, timedelta
from requests.exceptions import RequestException
from tweepy.errors import TweepyException

# Set up logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# Custom exception for API errors
class APIError(Exception):
    pass


# Discord bot setup
intents = discord.Intents.all()
intents.message_content = True
intents.members = True
intents.presences = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)


@bot.tree.command(name="list_tracked", description="List all tracked tokens")
async def slash_list_tracked(interaction: discord.Interaction):
    """List all tracked tokens using slash command"""
    if not tracked_tokens:
        await interaction.response.send_message("No tokens being tracked!")
        return
    response = "Tracked Tokens:\n" + "\n".join([
        f"{data['name'] or htag} ({htag}): {data['address'] or 'No address'}"
        for htag, data in tracked_tokens.items()
    ])
    await interaction.response.send_message(response)


@bot.tree.command(name="list_verified",
                  description="List verified influencers")
async def slash_list_verified(interaction: discord.Interaction):
    """List verified influencers using slash command"""
    if not verified_influencers:
        await interaction.response.send_message(
            "No verified influencers found!")
        return
    response = "Verified Influencers:\n" + "\n".join(
        [f"@{inf}" for inf in verified_influencers])
    await interaction.response.send_message(response)


@bot.tree.command(name="run", description="Run analysis on a token contract")
async def slash_run(interaction: discord.Interaction, contract: str):
    """Run analysis on a token contract"""
    try:
        # Validate contract and get token data
        token_name = validate_eth_contract(contract)
        if not token_name:
            token_name = validate_sol_contract(contract)

        if not token_name:
            await interaction.response.send_message(
                f"❌ Invalid contract address: {contract}")
            return

        # Get market data
        price_usd, volume_m5, market_cap, liquidity_usd = get_dex_data(
            contract)

        # Format response
        response = (f"🔍 Analysis for {token_name}\n"
                    f"Contract: {contract}\n"
                    f"Price: ${price_usd}\n"
                    f"5min Volume: ${volume_m5:,.2f}\n"
                    f"Market Cap: ${market_cap:,.2f}\n"
                    f"Liquidity: ${liquidity_usd:,.2f}")

        await interaction.response.send_message(response)

        # Add to tracked tokens if not already tracked
        hashtag = f"${token_name[:6].upper()}"
        if hashtag not in tracked_tokens:
            tracked_tokens[hashtag] = {
                'address': contract,
                'name': token_name,
                'last_count': 0,
                'last_m5_volume': volume_m5,
                'last_search_count': 0
            }

    except Exception as e:
        await interaction.response.send_message(
            f"❌ Error analyzing contract: {str(e)}")


# API Credentials from environment variables
X_API_KEY = os.getenv('X_API_KEY')
X_API_SECRET = os.getenv('X_API_SECRET')
X_ACCESS_TOKEN = os.getenv('X_ACCESS_TOKEN')
X_ACCESS_SECRET = os.getenv('X_ACCESS_SECRET')
X_BEARER_TOKEN = os.getenv('X_BEARER_TOKEN')
ETHERSCAN_API_KEY = os.getenv('ETHERSCAN_API_KEY')

if not all([
        X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET,
        X_BEARER_TOKEN, ETHERSCAN_API_KEY
]):
    logger.error("Missing required API credentials in environment variables")
    exit(1)

# Authenticate with X API v2
client = tweepy.Client(bearer_token=X_BEARER_TOKEN,
                       consumer_key=X_API_KEY,
                       consumer_secret=X_API_SECRET,
                       access_token=X_ACCESS_TOKEN,
                       access_token_secret=X_ACCESS_SECRET)

# Target X accounts and influencers
TARGET_ACCOUNTS = ['elonmusk', 'PicturesFolder', 'Ga__ke']
INFLUENCERS = ['Ga__ke', 'blknoiz06', 'kanyewest', 'shakira', '_Shadow36']

# Store tracked tokens: {hashtag: {'address': str, 'last_count': int, 'name': str, 'last_m5_volume': float, 'last_search_count': int}}
tracked_tokens = {}
last_posts = {}
verified_influencers = set()

# Thresholds
VIEW_SURGE_THRESHOLD = 10000
HASHTAG_SURGE_THRESHOLD = 50
VOLUME_SPIKE_THRESHOLD = 50000  # $50k volume change
SEARCH_SPIKE_THRESHOLD = 100  # 100+ mentions in 10 mins

# Regex patterns
HASHTAG_PATTERN = r'\$[A-Z]{3,6}'
ETH_ADDRESS_PATTERN = r'0x[a-fA-F0-9]{40}'
SOL_ADDRESS_PATTERN = r'[1-9A-HJ-NP-Za-km-z]{32,44}'


def validate_eth_contract(address):
    """Validate Ethereum contract and get token name"""
    try:
        url = f"https://api.etherscan.io/api?module=contract&action=getsourcecode&address={address}&apikey={ETHERSCAN_API_KEY}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data.get('status') == '1' and data.get('result',
                                                  [{}])[0].get('ContractName'):
            token_url = f"https://api.etherscan.io/api?module=token&action=tokeninfo&contractaddress={address}&apikey={ETHERSCAN_API_KEY}"
            token_response = requests.get(token_url, timeout=10)
            token_response.raise_for_status()
            token_data = token_response.json()

            if token_data.get('status') == '1' and token_data.get('result'):
                return token_data['result'][0].get('tokenName',
                                                   'Unknown Token')
        return None
    except RequestException as e:
        logger.error(f"Error validating ETH contract {address}: {str(e)}")
        raise APIError(f"Failed to validate ETH contract: {str(e)}")


def validate_sol_contract(address):
    """Validate Solana token and get name"""
    url = f"https://public-api.solscan.io/token/meta/{address}"
    response = requests.get(url).json()
    if 'name' in response and response['name']:
        return response['name']
    return None


def get_dex_data(address):
    """Fetch price, 5-min volume, market cap, and liquidity from DEXScreener"""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
    try:
        response = requests.get(url).json()
        if response.get('pairs') and len(response['pairs']) > 0:
            pair = response['pairs'][0]
            price_usd = pair.get('priceUsd', 'N/A')
            volume_m5 = float(pair.get('volume', {}).get('m5', 0))
            market_cap = float(pair.get('fdv', 0)) if pair.get('fdv') else 0
            liquidity_usd = float(pair.get('liquidity', {}).get(
                'usd', 0)) if pair.get('liquidity') else 0
            return price_usd, volume_m5, market_cap, liquidity_usd
        return 'N/A', 0, 0, 0
    except Exception as e:
        print(f"DEXScreener error for {address}: {e}")
        return 'N/A', 0, 0, 0


def check_verified_influencers():
    """Populate verified_influencers with verified accounts"""
    global verified_influencers
    try:
        users = client.get_users(usernames=INFLUENCERS,
                                 user_fields=['verified'])
        for user in users.data:
            if user.verified:
                verified_influencers.add(user.username)
    except Exception as e:
        print(f"Error checking verified influencers: {e}")


@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    await bot.tree.sync()
    print("Slash commands synced")
    check_verified_influencers()
    monitor_x.start()


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # Extract all potential contract addresses
    eth_addresses = re.findall(ETH_ADDRESS_PATTERN, message.content)
    sol_addresses = re.findall(SOL_ADDRESS_PATTERN, message.content)
    hashtags = re.findall(HASHTAG_PATTERN, message.content)

    # Process all found addresses immediately
    for address in eth_addresses + sol_addresses:
        try:
            # Try Ethereum first
            token_name = validate_eth_contract(address)
            if token_name:
                hashtag = f"${token_name[:6].upper()}"
                tracked_tokens[hashtag] = {
                    'address': address,
                    'name': token_name,
                    'last_count': 0,
                    'last_m5_volume': 0,
                    'last_search_count': 0
                }
                await message.channel.send(
                    f"✅ Found ETH token: {token_name} ({hashtag}) at {address}"
                )
                continue

            # If not ETH, try Solana
            token_name = validate_sol_contract(address)
            if token_name:
                hashtag = f"${token_name[:6].upper()}"
                tracked_tokens[hashtag] = {
                    'address': address,
                    'name': token_name,
                    'last_count': 0,
                    'last_m5_volume': 0,
                    'last_search_count': 0
                }
                await message.channel.send(
                    f"✅ Found SOL token: {token_name} ({hashtag}) at {address}"
                )
                continue

        except Exception as e:
            await message.channel.send(
                f"❌ Error processing address {address}: {str(e)}")
            continue

    # Process hashtags if they're not already tracked
    if hashtags:
        for hashtag in hashtags:
            if hashtag not in tracked_tokens:
                tracked_tokens[hashtag] = {
                    'address': None,
                    'name': None,
                    'last_count': 0,
                    'last_m5_volume': 0,
                    'last_search_count': 0
                }
                await message.channel.send(
                    f"👀 Tracking {hashtag} (no contract yet)")

            if hashtag not in tracked_tokens:
                if address and token_name:
                    tracked_tokens[hashtag] = {
                        'address': address,
                        'last_count': 0,
                        'name': token_name,
                        'last_m5_volume': 0,
                        'last_search_count': 0
                    }
                    await message.channel.send(
                        f"Tracking {token_name} ({hashtag}) at {address}")
                else:
                    tracked_tokens[hashtag] = {
                        'address': address,
                        'last_count': 0,
                        'name': None,
                        'last_m5_volume': 0,
                        'last_search_count': 0
                    }
                    await message.channel.send(
                        f"Tracking {hashtag} {'at ' + address if address else ''} (invalid or no contract)"
                    )
            elif address and not tracked_tokens[hashtag]['address']:
                if token_name:
                    tracked_tokens[hashtag]['address'] = address
                    tracked_tokens[hashtag]['name'] = token_name
                    await message.channel.send(
                        f"Linked {token_name} ({hashtag}) to {address}")
                else:
                    await message.channel.send(
                        f"Invalid contract for {hashtag}: {address}")


# Rate limiting variables
RATE_LIMIT_DELAY = 120  # 2 minutes base delay
current_delay = RATE_LIMIT_DELAY
MAX_DELAY = 600  # 10 minutes maximum delay


async def handle_rate_limit():
    global current_delay
    current_delay = min(current_delay * 2, MAX_DELAY)
    logger.warning(f"Rate limit hit, waiting {current_delay} seconds")
    await asyncio.sleep(current_delay)


async def safe_twitter_request(func, *args, **kwargs):
    global current_delay
    try:
        result = func(*args, **kwargs)
        current_delay = RATE_LIMIT_DELAY  # Reset delay on success
        return result
    except TweepyException as e:
        if '429' in str(e):
            await handle_rate_limit()
        logger.error(f"Twitter API error: {str(e)}")
        return None


@tasks.loop(seconds=120)  # Increased base interval to 2 minutes
async def monitor_x():
    try:
        channel = bot.get_channel(1356350810191036678)
        if not channel:
            logger.error("Discord channel not found!")
            return

        await process_monitoring(channel)
    except Exception as e:
        logger.error(f"Critical error in monitor_x: {str(e)}")
        await asyncio.sleep(current_delay)


async def process_monitoring(channel):

    # 1. View surges
    for username in TARGET_ACCOUNTS:
        try:
            user = await safe_twitter_request(client.get_user,
                                              username=username)
            if not user or not user.data:
                logger.warning(f"User {username} not found")
                continue
            user_id = user.data.id
            tweets = client.get_users_tweets(
                id=user_id,
                max_results=5,
                tweet_fields=['public_metrics', 'created_at'])
            if not tweets.data:
                continue

            latest_tweet = tweets.data[0]
            tweet_id = latest_tweet.id
            view_count = latest_tweet.public_metrics.get('impression_count', 0)
            tweet_text = latest_tweet.text

            if username not in last_posts or last_posts[username][
                    'id'] != tweet_id:
                if view_count >= VIEW_SURGE_THRESHOLD:
                    await channel.send(
                        f"🚨 @{username} View Surge: {view_count} views on '{tweet_text[:50]}...'"
                    )
                last_posts[username] = {'id': tweet_id, 'views': view_count}
            elif view_count >= last_posts[username]['views'] * 5:
                await channel.send(
                    f"🚨 @{username} Views Spiked: {last_posts[username]['views']} -> {view_count}"
                )
                last_posts[username]['views'] = view_count

        except Exception as e:
            print(f"Error with {username}: {e}")

    # 2. Hashtag surges, verified influencers, price, volume, market cap, liquidity, and search spikes
    for hashtag, data in tracked_tokens.items():
        try:
            # Hashtag frequency (2 mins)
            end_time = datetime.utcnow()
            start_time = end_time - timedelta(minutes=2)
            hashtag_counts = client.get_recent_tweets_count(
                query=hashtag,
                granularity='minute',
                start_time=start_time,
                end_time=end_time)
            recent_count = sum([
                count.tweet_count for count in hashtag_counts.data
            ]) if hashtag_counts.data else 0

            # Search spike (10 mins)
            search_end_time = datetime.utcnow()
            search_start_time = search_end_time - timedelta(minutes=10)
            search_counts = client.get_recent_tweets_count(
                query=hashtag,
                granularity='minute',
                start_time=search_start_time,
                end_time=search_end_time)
            search_count = sum(
                [count.tweet_count
                 for count in search_counts.data]) if search_counts.data else 0
            search_spike = search_count - data.get('last_search_count', 0)

            # Price, volume, market cap, and liquidity
            price_usd = 'N/A'
            volume_m5 = 0
            market_cap = 0
            liquidity_usd = 0
            volume_change = 0
            volume_trend = 'N/A'
            if data['address']:
                price_usd, volume_m5, market_cap, liquidity_usd = get_dex_data(
                    data['address'])
                if 'last_m5_volume' in data and volume_m5 != 0 and data[
                        'last_m5_volume'] != 0:
                    volume_change = (volume_m5 - data['last_m5_volume']) * (
                        2 / 5)  # 2-min estimate
                    volume_trend = 'Bullish' if volume_change > 0 else 'Bearish'

            # Surge alert
            if (recent_count >= HASHTAG_SURGE_THRESHOLD
                    or recent_count > data['last_count'] * 2
                    or abs(volume_change) >= VOLUME_SPIKE_THRESHOLD
                    or search_spike >= SEARCH_SPIKE_THRESHOLD):
                token_display = f"{data['name']} ({hashtag})" if data[
                    'name'] else hashtag
                alert = (
                    f"🚀 {token_display} Surge: {recent_count} mentions in last 2 mins (prev: {data['last_count']})\n"
                    f"Price: ${price_usd}\n"
                    f"Volume Spike (2m): {'+' if volume_change > 0 else ''}{volume_change:.2f} USD ({volume_trend})\n"
                    f"Market Cap: ${market_cap:,.2f}\n"
                    f"Liquidity: ${liquidity_usd:,.2f}\n"
                    f"Search Spike (10m): +{search_spike} mentions\n"
                    f"Contract: {data['address'] if data['address'] else 'Not provided'}"
                )
                await channel.send(alert)
            tracked_tokens[hashtag]['last_count'] = recent_count
            tracked_tokens[hashtag]['last_search_count'] = search_count
            if data['address']:
                tracked_tokens[hashtag]['last_m5_volume'] = volume_m5

            # Verified influencer posts
            influencer_query = f"{hashtag} from:{' from:'.join(verified_influencers)}"
            influencer_tweets = client.search_recent_tweets(
                query=influencer_query,
                max_results=10,
                tweet_fields=['created_at'])
            if influencer_tweets.data:
                influencer_count = len(influencer_tweets.data)
                influencer_alert = (
                    f"🌟 {token_display} Verified Influencer Posts: {influencer_count} in last 2 mins\n"
                    f"Price: ${price_usd}\n"
                    f"Volume Spike (2m): {'+' if volume_change > 0 else ''}{volume_change:.2f} USD ({volume_trend})\n"
                    f"Market Cap: ${market_cap:,.2f}\n"
                    f"Liquidity: ${liquidity_usd:,.2f}\n"
                    f"Search Spike (10m): +{search_spike} mentions")
                for tweet in influencer_tweets.data:
                    influencer_alert += f"\n- @{tweet.author_id}: '{tweet.text[:50]}...'"
                await channel.send(influencer_alert)

        except Exception as e:
            print(f"Error with {hashtag}: {e}")


@bot.command()
async def list_tracked(ctx):
    """List all tracked tokens"""
    if not tracked_tokens:
        await ctx.send("No tokens being tracked!")
        return
    response = "Tracked Tokens:\n" + "\n".join([
        f"{data['name'] or htag} ({htag}): {data['address'] or 'No address'}"
        for htag, data in tracked_tokens.items()
    ])
    await ctx.send(response)


@bot.command()
async def list_verified(ctx):
    """List verified influencers"""
    if not verified_influencers:
        await ctx.send("No verified influencers found!")
        return
    response = "Verified Influencers:\n" + "\n".join(
        [f"@{inf}" for inf in verified_influencers])
    await ctx.send(response)


# Run the bot
import os

token = os.getenv('DISCORD_TOKEN')
if not token:
    print("Error: Discord token not found in environment variables")
    exit(1)
bot.run(token)
