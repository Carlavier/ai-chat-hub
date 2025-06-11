import os
import streamlit as st
import redis
from openai import OpenAI
from dotenv import load_dotenv
import json
import random
from datetime import datetime
import time

# Load environment
load_dotenv()

# Initialize Redis connection (without singleton)


def init_redis():
    return redis.Redis(
        host=os.getenv("REDIS_PUBLIC_ENDPOINT"),
        port=os.getenv("REDIS_PORT"),
        # Add this if your Redis requires a username
        username=os.getenv("REDIS_USERNAME"),
        # Add this if your Redis requires a password
        password=os.getenv("REDIS_PASSWORD"),
        decode_responses=True
    )


try:
    redis_client = init_redis()
    redis_client.ping()  # Test connection
except Exception as e:
    st.error(f"Redis connection error: {str(e)}")
    st.stop()

# Initialize OpenAI client
ai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- Constants ---
USER_CHAT_CHANNEL = "user_chat"
USER_EXPIRE_SECONDS = 30

# --- Bot Configuration ---
BOT_PROFILES = {
    "Jester": {
        "system_prompt": "You are a witty comedian bot. Respond with humor and jokes. Keep responses under 2 sentences. Never be serious. Never break character.",
        "avatar": "🤡",
        "temperature": 1.0
    },
    "Philosopher": {
        "system_prompt": "You are a hardcore philosopher. You want to find the pattern hiding in the chaos. Use formal language. Never use contractions. Keep responses under 3 sentences. Never break character.",
        "avatar": "🎓",
        "temperature": 0.3
    }
}

# --- Helper Functions ---


def send_user_message(sender, message):
    msg_data = {
        "sender": sender,
        "text": message,
        "is_bot": False,
        "timestamp": str(datetime.now())
    }
    redis_client.setex(f"user:{sender}", USER_EXPIRE_SECONDS, "active")
    redis_client.rpush("chat_history", json.dumps(msg_data))
    redis_client.ltrim("chat_history", -100, -1)  # Keep only last 100 messages


def get_active_users():
    return [key.split(":")[1] for key in redis_client.keys("user:*")]


def get_chat_history():
    history = redis_client.lrange("chat_history", 0, -1)
    return [json.loads(item) for item in history if item]

# --- Streamlit App ---


def main():
    st.title("💬 Real-Time Chat Hub")

    # Initialize session state
    if "user" not in st.session_state:
        st.session_state.user = f"User_{random.randint(1000, 9999)}"

    tab1, tab2 = st.tabs(["Real-Time User Chat", "Bot Arena"])

    # --- Tab 1: Real-Time User Chat ---
    with tab1:
        # Sidebar controls
        with st.sidebar:
            st.session_state.user = st.text_input(
                "Your Name", st.session_state.user)
            st.markdown("### 👥 Active Users")
            active_users = get_active_users()
            st.write(", ".join(active_users) or "No active users")

        # Message input
        user_input = st.chat_input("Type your message...")
        if user_input:
            send_user_message(st.session_state.user, user_input)
            st.rerun()

        # Display chat history
        chat_history = get_chat_history()
        for msg in reversed(chat_history[-20:]):  # Show last 20 messages
            with st.chat_message(msg["sender"]):
                st.write(msg["text"])
                st.caption(msg["timestamp"])

    # --- Tab 2: Bot Arena ---
    with tab2:
        st.subheader("🤖 Bot Arena")
        # Two bots talk to each other in real-time, user observes only
        bot_names = list(BOT_PROFILES.keys())
        bot1, bot2 = bot_names[0], bot_names[1]

        # Redis key for bot arena chat
        BOT_ARENA_HISTORY_KEY = "bot_arena_history"
        BOT_ARENA_MAX_MSGS = 30

        def get_bot_arena_history():
            history = redis_client.lrange(BOT_ARENA_HISTORY_KEY, 0, -1)
            return [json.loads(item) for item in history if item]

        def add_bot_arena_message(sender, text):
            msg_data = {
            "sender": sender,
            "text": text,
            "is_bot": True,
            "timestamp": str(datetime.now())
            }
            redis_client.rpush(BOT_ARENA_HISTORY_KEY, json.dumps(msg_data))
            redis_client.ltrim(BOT_ARENA_HISTORY_KEY, -BOT_ARENA_MAX_MSGS, -1)

        # Generate next bot message if needed
        history = get_bot_arena_history()
        BOT_ARENA_DELAY_SECONDS = 15  # Increase delay to slow down bot conversation
        if not history:
            # Start the conversation
            add_bot_arena_message(bot1, "Hello, let's start a conversation!")
            st.rerun()
        else:
            last_msg = history[-1]
            # Alternate bots
            next_bot = bot2 if last_msg["sender"] == bot1 else bot1
            # Only generate if last message is older than BOT_ARENA_DELAY_SECONDS
            last_time = datetime.fromisoformat(last_msg["timestamp"])
            if (datetime.now() - last_time).total_seconds() > BOT_ARENA_DELAY_SECONDS:
                # Compose prompt
                system_prompt = BOT_PROFILES[next_bot]["system_prompt"]
                temperature = BOT_PROFILES[next_bot]["temperature"]
                # Build conversation for context
                messages = [
                    {"role": "system", "content": system_prompt}
                ]
                for msg in history[-6:]:
                    role = "assistant" if msg["sender"] == next_bot else "user"
                    messages.append({"role": role, "content": msg["text"]})
                # Get bot response
                try:
                    response = ai_client.chat.completions.create(
                        model="gpt-3.5-turbo",
                        messages=messages,
                        temperature=temperature,
                        max_tokens=80,
                    )
                    bot_reply = response.choices[0].message.content.strip()
                except Exception as e:
                    bot_reply = f"(Error: {e})"
                add_bot_arena_message(next_bot, bot_reply)
                st.rerun()

        # Display bot conversation (newest at top)
        st.info("You are observing the bots. You cannot send messages in this arena.")
        arena_history = get_bot_arena_history()[-20:]
        for msg in reversed(arena_history):  # Reverse to show newest at top
            avatar = BOT_PROFILES.get(msg["sender"], {}).get("avatar", "🤖")
            with st.chat_message(msg["sender"], avatar=avatar):
                st.write(msg["text"])
                st.caption(msg["timestamp"])

    # Auto-refresh every 2 seconds
    time.sleep(2)
    st.rerun()


if __name__ == "__main__":
    main()
