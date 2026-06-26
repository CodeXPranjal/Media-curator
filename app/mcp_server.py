from mcp.server.fastmcp import FastMCP

app = FastMCP("media_curator_mcp")

@app.tool()
async def search_goodreads(title: str) -> str:
    """Searches for a book by title and returns its summary and rating."""
    return f"Book: {title}\nRating: 4.5/5\nSummary: A highly recommended book that matches the user's mood."

@app.tool()
async def search_tmdb(title: str) -> str:
    """Searches for a movie or TV show by title and returns its summary and rating."""
    return f"Movie/Show: {title}\nRating: 8.5/10\nSummary: A trending title that perfectly fits the available time and mood."

@app.tool()
async def check_streaming_availability(title: str) -> str:
    """Checks which streaming platforms currently have the title."""
    return f"{title} is available on Netflix and Hulu."

if __name__ == "__main__":
    app.run(transport="stdio")
