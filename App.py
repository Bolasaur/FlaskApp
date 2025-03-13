from flask import Flask, render_template_string , request, render_template, redirect, url_for
import pandas as pd
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials

app = Flask(__name__)

def update_data():
    global matchup_data_file_path, effectiveness_scores_file_path
    global matchup_data_df, effectiveness_scores_df
    global effectiveness_scores, max_card_copies
    global matchup_data, total_games_played

    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name("google_sheets_credentials.json", scope)
    client = gspread.authorize(creds)

    matchup_data_file_path = client.open("Matchup_Data_Cloud")
    sheet1 = matchup_data_file_path.sheet1
    effectiveness_scores_file_path = client.open("Effectiveness_Scores_Cloud")
    sheet2 = effectiveness_scores_file_path.sheet1

    matchup_data_df = pd.DataFrame(sheet1.get_all_records())
    effectiveness_scores_df = pd.DataFrame(sheet2.get_all_records())

    effectiveness_scores = {}
    max_card_copies = {}

    for _, row in effectiveness_scores_df.iterrows():
        card_name = row.iloc[0]  # Assuming first column is the card name
        effectiveness_scores[card_name] = row.iloc[1:].to_dict()
        max_card_copies[card_name] = int(row["Max Copies"]) if "Max Copies" in row and not pd.isna(row["Max Copies"]) else 4

    # === Bayesian Adjustments for Play Rate & Win Rate ===
    matchup_data = {}
    total_games_played = matchup_data_df["# of times fought"].sum()

    for _, row in matchup_data_df.iterrows():
        deck_name = row["Deck"]
        recorded_playrate = row["# of times fought"] / total_games_played
        expected_playrate = row["MTGO PR"]
        times_fought = max(1, row["# of times fought"])  # Prevent dividing by 0
        max_slots = row["Max Slots"]
        recorded_winrate = row["# of match wins"] / times_fought

        # Bayesian adjustment using an inversely proportional K factor
        adjusted_playrate = ((times_fought) + (expected_playrate * (5 / total_games_played))) / (total_games_played + (5 / total_games_played))
        adjusted_winrate = ((recorded_winrate) * (times_fought) + (0.5 * (30 / times_fought))) / ((30 / times_fought) + times_fought)

        matchup_data[deck_name] = {
            "adjusted_playrate": adjusted_playrate,
            "adjusted_winrate": adjusted_winrate,
            "max_slots": max_slots
        }



def assign_sideboard_cards(remaining_slots):
    sideboard_map = {}
    A = 0.7  # 70% priority to play rate
    B = 0.3  # 30% priority to bad matchups

    sorted_decks = sorted(
        matchup_data.items(),
        key=lambda x: (x[1]["adjusted_playrate"] * A) + ((0.50 - x[1]["adjusted_winrate"]) * B),
        reverse=True  # Higher priority scores first
    )

    for deck_name, data in sorted_decks:
        if remaining_slots <= 0:
            break

        sorted_cards = sorted(
            effectiveness_scores.keys(),
            key=lambda card: effectiveness_scores[card].get(deck_name, 0),
            reverse=True
        )

        for card in sorted_cards:
            if remaining_slots <= 0:
                break

            max_allowed = min(4, remaining_slots, max_card_copies.get(card, 4))
            num_copies = min(max_allowed, data["max_slots"])

            if num_copies > 0:
                sideboard_map[card] = num_copies
                remaining_slots -= num_copies

    return sideboard_map

def refine_sideboard(sideboard_map):
    max_iterations = 100  # Failsafe to avoid infinite loops
    penalty_tracker = {}
    seen_sideboards = set()

    for iteration in range(max_iterations):
        previous_sideboard = sideboard_map.copy()
        sideboard_tuple = tuple(sorted(sideboard_map.items()))

        if sideboard_tuple in seen_sideboards:
            break
        seen_sideboards.add(sideboard_tuple)

        removable_cards = {}  
        boardable_per_matchup = {}
        dead_cards = []

        for deck, data in matchup_data.items():
            boardable_per_matchup[deck] = sum(
                sideboard_map.get(card, 0) for card in sideboard_map 
                if effectiveness_scores[card].get(deck, 0) > 5
            )
            max_boardable = data["max_slots"]
            
            if boardable_per_matchup[deck] > max_boardable:
                excess = boardable_per_matchup[deck] - max_boardable
                removable_cards[deck] = sorted(
                    (card for card in sideboard_map if effectiveness_scores[card].get(deck, 0) > 5),
                    key=lambda c: effectiveness_scores[c][deck]
                )[:max(1, excess // 3)]  # Slower removal to prevent over-trimming

        # Identify dead cards (low impact across matchups)
        for card in list(sideboard_map.keys()):
            impacted_matchups = sum(1 for deck in matchup_data if effectiveness_scores[card].get(deck, 0) > 5)
            if impacted_matchups <= 2:  # Remove cards that are only useful in <=2 matchups
                dead_cards.append(card)
        
        for deck, cards in removable_cards.items():
            for card in cards:
                if card in sideboard_map and sideboard_map[card] > 0:
                    sideboard_map[card] -= 1
                    if sideboard_map[card] == 0:
                        del sideboard_map[card]
                    penalty_tracker[card] = penalty_tracker.get(card, 0) + 1

        for card in dead_cards:
            if card in sideboard_map:
                del sideboard_map[card]

        # Ensure sideboard refills after removals
        remaining_slots = 15 - sum(sideboard_map.values())
        if remaining_slots > 0:
            additional_cards = sorted(
                effectiveness_scores.keys(),
                key=lambda c: (sum(effectiveness_scores[c].values()) - penalty_tracker.get(c, 0)) + sum(1 for deck in matchup_data if effectiveness_scores[c].get(deck, 0) > 5) * 3,
                reverse=True
            )

            for card in additional_cards:
                if remaining_slots <= 0:
                    break
                if card not in sideboard_map or sideboard_map[card] < max_card_copies[card]:
                    sideboard_map[card] = sideboard_map.get(card, 0) + 1
                    remaining_slots -= 1
    
    return sideboard_map

@app.route("/")
def home():
    update_data()
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>MTG Sideboard App</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body {
                background-color: #f8f9fa;
                font-family: Arial, sans-serif;
                text-align: center;
                padding: 20px;
            }
            .container {
                max-width: 500px;
                margin: auto;
                padding: 20px;
                background: white;
                border-radius: 10px;
                box-shadow: 0px 4px 8px rgba(0, 0, 0, 0.2);
            }
            .btn {
                width: 100%;
                margin: 5px 0;
            }
            #back-button {
                display: none;
            }
            #edit-options {
                display: none;
            }
        </style>
        <script>
            function showEditOptions() {
                document.getElementById('edit-options').style.display = 'block';
                document.getElementById('back-button').style.display = 'block';
                document.getElementById('main-options').style.display = 'none';
            }
            function goBack() {
                document.getElementById('edit-options').style.display = 'none';
                document.getElementById('back-button').style.display = 'none';
                document.getElementById('main-options').style.display = 'block';
            }
        </script>
    </head>
    <body>
        <div class="container">
            <h1 class="mb-3">MTG Sideboard App</h1>
            <p class="text-muted">Choose an option:</p>

            <div id="main-options">
                <button class="btn btn-primary" onclick="location.href='/sideboard'">Run Sideboard Optimizer</button>
                <button class="btn btn-secondary" onclick="showEditOptions()">Edit Data</button>
            </div>

            <!-- Back Button -->
            <button id="back-button" class="btn btn-danger" onclick="goBack()">Back</button>

            <div id="edit-options">
                <h2 class="mt-3">Edit Data</h2>
                <button class="btn btn-outline-primary" onclick="location.href='/add_card'">Add Card</button>
                <button class="btn btn-outline-primary" onclick="location.href='/add_deck'">Add Deck</button>
                <button class="btn btn-outline-primary" onclick="location.href='/add_match'">Add Match Record</button>
                <button class="btn btn-outline-danger" onclick="location.href='/remove_deck'">Remove Deck</button>
                <button class="btn btn-outline-danger" onclick="location.href='/remove_card'">Remove Card</button>
                <button class="btn btn-outline-info" onclick="location.href='/view_decks'">View Decks</button>
                <button class="btn btn-outline-info" onclick="location.href='/view_cards'">View Cards</button>
            </div>
        </div>

        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    </body>
    </html>
    """
    return render_template_string(html)

@app.route("/add_card", methods=["GET", "POST"])
def add_card():
    global effectiveness_scores_file_path  # This is still your Google Sheet object

    # Read data from Google Sheets to get deck names
    update_data()
    deck_names = list(effectiveness_scores_df.columns[2:])  # Ignore first two columns

    if request.method == "POST":
        card_name = request.form.get("card_name")
        max_copies = int(request.form.get("max_copies"))

        # Get effectiveness scores from the form
        effectiveness_values = []
        for deck in deck_names:
            value = int(request.form.get(f"effectiveness[{deck}]"))
            effectiveness_values.append(value)

        # Append new row to Google Sheet
        new_row = [card_name, max_copies] + effectiveness_values
        sheet = effectiveness_scores_file_path.sheet1  # Get the first sheet
        sheet.append_row(new_row, value_input_option="USER_ENTERED")

        return redirect(url_for("home"))  # Redirect to home page after adding

    # Define the enhanced HTML layout
    add_card_html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Add Card</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body {
                background-color: #f8f9fa;
                font-family: Arial, sans-serif;
                padding: 20px;
            }
            .container {
                max-width: 600px;
                margin: auto;
                padding: 20px;
                background: white;
                border-radius: 10px;
                box-shadow: 0px 4px 8px rgba(0, 0, 0, 0.2);
            }
            .btn-primary {
                width: 100%;
                margin-top: 15px;
            }
            .form-group {
                margin-bottom: 15px;
            }
            h1 {
                text-align: center;
                margin-bottom: 20px;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Add a New Sideboard Card</h1>
            <form action="{{ url_for('add_card') }}" method="post">
                <div class="form-group">
                    <label for="card_name" class="form-label">Card Name:</label>
                    <input type="text" class="form-control" id="card_name" name="card_name" required>
                </div>

                <div class="form-group">
                    <label for="max_copies" class="form-label">Max Copies Allowed:</label>
                    <input type="number" class="form-control" id="max_copies" name="max_copies" required>
                </div>

                <h3 class="mt-3">Enter Effectiveness Scores (1-10):</h3>
                {% for deck in deck_names %}
                <div class="form-group">
                    <label for="{{ deck }}" class="form-label">{{ deck }}:</label>
                    <input type="number" class="form-control" id="{{ deck }}" name="effectiveness[{{ deck }}]" min="0" max="10" required>
                </div>
                {% endfor %}

                <button type="submit" class="btn btn-primary">Add Card</button>
                <a href="{{ url_for('home') }}" class="btn btn-secondary mt-2">Back to Home</a>
            </form>
        </div>

        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    </body>
    </html>
    """

    return render_template_string(add_card_html, deck_names=deck_names)


@app.route("/add_deck", methods=["GET", "POST"])
def add_deck():
    global effectiveness_scores_file_path, matchup_data_file_path

    # Read existing data from Google Sheets
    update_data()

    if request.method == "POST":
        # Get user inputs from the form
        deck_name = request.form.get("deck_name")
        mtgo_pr = float(request.form.get("mtgo_pr"))
        max_slots = int(request.form.get("max_slots"))

        # Add new deck to Matchup_Data_Cloud
        new_deck_row = [deck_name, mtgo_pr, max_slots, 0, 0]
        sheet1 = matchup_data_file_path.sheet1  # Get first sheet of Matchup_Data_Cloud
        sheet1.append_row(new_deck_row, value_input_option="USER_ENTERED")

        # Get effectiveness scores for the new deck
        effectiveness_values = []
        for index, row in effectiveness_scores_df.iterrows():
            value = int(request.form.get(f"effectiveness[{row['Card Name']}]"))
            effectiveness_values.append(value)

        # Add the new deck as a column in Effectiveness_Scores_Cloud
        sheet2 = effectiveness_scores_file_path.sheet1  # Get first sheet of Effectiveness_Scores_Cloud
        all_data = sheet2.get_all_values()  # Fetch all values to preserve structure

        # Insert new column with effectiveness scores
        if all_data:
            for i in range(1, len(all_data)):  # Skip header row
                all_data[i].append(str(effectiveness_values[i - 1]))

            # Add deck name to the header row
            all_data[0].append(deck_name)

            # Update the entire sheet in one batch operation
            sheet2.update(all_data)

        return redirect(url_for("home"))  # Redirect to home page after adding

    # Enhanced HTML Layout
    add_deck_html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Add Deck</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body {
                background-color: #f8f9fa;
                font-family: Arial, sans-serif;
                padding: 20px;
            }
            .container {
                max-width: 600px;
                margin: auto;
                padding: 20px;
                background: white;
                border-radius: 10px;
                box-shadow: 0px 4px 8px rgba(0, 0, 0, 0.2);
            }
            .btn-primary {
                width: 100%;
                margin-top: 15px;
            }
            .form-group {
                margin-bottom: 15px;
            }
            h1 {
                text-align: center;
                margin-bottom: 20px;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Add a New Deck</h1>
            <form action="{{ url_for('add_deck') }}" method="post">
                <div class="form-group">
                    <label for="deck_name" class="form-label">Deck Name:</label>
                    <input type="text" class="form-control" id="deck_name" name="deck_name" required>
                </div>

                <div class="form-group">
                    <label for="mtgo_pr" class="form-label">MTGO PR (Play Rate Estimation):</label>
                    <input type="number" class="form-control" id="mtgo_pr" name="mtgo_pr" step="0.01" required>
                </div>

                <div class="form-group">
                    <label for="max_slots" class="form-label">Max Sideboard Slots:</label>
                    <input type="number" class="form-control" id="max_slots" name="max_slots" required>
                </div>

                <h3 class="mt-3">Enter Effectiveness Scores (1-10):</h3>
                {% for card in card_names %}
                <div class="form-group">
                    <label for="{{ card }}" class="form-label">{{ card }}:</label>
                    <input type="number" class="form-control" id="{{ card }}" name="effectiveness[{{ card }}]" min="0" max="10" required>
                </div>
                {% endfor %}

                <button type="submit" class="btn btn-primary">Add Deck</button>
                <a href="{{ url_for('home') }}" class="btn btn-secondary mt-2">Back to Home</a>
            </form>
        </div>

        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    </body>
    </html>
    """

    return render_template_string(add_deck_html, card_names=effectiveness_scores_df["Card Name"].tolist())

@app.route("/add_match", methods=["GET", "POST"])
def add_match():
    global matchup_data_file_path

    # Load existing matchup data from Google Sheets
    update_data()
    sheet = matchup_data_file_path.sheet1  # Get the first sheet

    # Read the data from Google Sheets
    matchup_data_list = sheet.get_all_values()
    header = matchup_data_list[0]  # First row (column names)
    data_rows = matchup_data_list[1:]  # All remaining rows (actual data)

    if request.method == "POST":
        # Get user inputs from the form
        deck_name = request.form.get("deck_name")
        match_result = request.form.get("match_result")  # Expected format: "2-0", "1-2", etc.

        # Ensure the deck exists
        deck_names = [row[0] for row in data_rows]  # Extract deck names from first column
        if deck_name not in deck_names:
            return f"<h1>Error</h1><p>Deck '{deck_name}' not found. Please add it first.</p>"

        # Parse match result
        try:
            wins, losses = map(int, match_result.split("-"))
        except ValueError:
            return "<h1>Error</h1><p>Invalid match result format. Please enter in 'X-Y' format.</p>"

        # Determine if the match was won (if wins > losses, it's a match win)
        match_win = 1 if wins > losses else 0

        # Find the row index of the deck
        row_index = deck_names.index(deck_name) + 2  # +2 because of 1-based index and header row

        # Column indices (1-based indexing for Google Sheets)
        col_fought = header.index("# of times fought") + 1
        col_wins = header.index("# of match wins") + 1

        # Read existing values
        current_fought = int(sheet.cell(row_index, col_fought).value)
        current_wins = int(sheet.cell(row_index, col_wins).value)

        # Update values
        new_fought = current_fought + 1
        new_wins = current_wins + match_win

        # Batch update to avoid multiple calls
        sheet.update_cell(row_index, col_fought, new_fought)
        sheet.update_cell(row_index, col_wins, new_wins)

        return redirect(url_for("home"))  # Redirect to home after adding match

    # Enhanced HTML template
    add_match_html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Add Match Record</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body {
                background-color: #f8f9fa;
                font-family: Arial, sans-serif;
                padding: 20px;
            }
            .container {
                max-width: 500px;
                margin: auto;
                padding: 20px;
                background: white;
                border-radius: 10px;
                box-shadow: 0px 4px 8px rgba(0, 0, 0, 0.2);
            }
            .btn-primary {
                width: 100%;
                margin-top: 15px;
            }
            .form-group {
                margin-bottom: 15px;
            }
            h1 {
                text-align: center;
                margin-bottom: 20px;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Record a Match Result</h1>
            <form action="{{ url_for('add_match') }}" method="post">
                <div class="form-group">
                    <label for="deck_name" class="form-label">Deck Played Against:</label>
                    <select class="form-select" id="deck_name" name="deck_name" required>
                        {% for deck in deck_names %}
                            <option value="{{ deck }}">{{ deck }}</option>
                        {% endfor %}
                    </select>
                </div>

                <div class="form-group">
                    <label for="match_result" class="form-label">Match Result (e.g., 2-0, 1-2):</label>
                    <input type="text" class="form-control" id="match_result" name="match_result" pattern="\\d+-\\d+" required>
                    <small class="form-text text-muted">Enter the result in X-Y format.</small>
                </div>

                <button type="submit" class="btn btn-primary">Record Match</button>
                <a href="{{ url_for('home') }}" class="btn btn-secondary mt-2">Back to Home</a>
            </form>
        </div>

        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    </body>
    </html>
    """

    return render_template_string(add_match_html, deck_names=[row[0] for row in data_rows])


@app.route("/remove_deck", methods=["GET", "POST"])
def remove_deck():
    global matchup_data_file_path, effectiveness_scores_file_path

    # Load the latest versions of both Google Sheets
    update_data()

    sheet1 = matchup_data_file_path.sheet1  # Matchup Data
    sheet2 = effectiveness_scores_file_path.sheet1  # Effectiveness Scores

    # Extract deck names from Effectiveness Scores (columns after first two)
    all_data = sheet2.get_all_values()
    deck_names = all_data[0][2:] if all_data else []  # Extract headers after first two columns

    if request.method == "POST":
        deck_name = request.form.get("deck_name")  # Get selected deck

        # Ensure the deck exists
        if deck_name not in deck_names:
            return f"<h1>Error</h1><p>Deck '{deck_name}' not found in Effectiveness_Scores_Cloud.</p>"
        
        # Load Matchup Data
        matchup_data_list = sheet1.get_all_values()
        header = matchup_data_list[0]
        data_rows = matchup_data_list[1:]

        deck_names_matchup = [row[0] for row in data_rows]  # Deck names from the first column

        if deck_name not in deck_names_matchup:
            return f"<h1>Error</h1><p>Deck '{deck_name}' not found in Matchup_Data_Cloud.</p>"

        ### **STEP 1: Remove Deck (Row) from Matchup_Data_Cloud**
        row_index = deck_names_matchup.index(deck_name) + 2  # +2 to adjust for 1-based indexing and header
        sheet1.delete_rows(row_index)

        ### **STEP 2: Remove Deck (Column) from Effectiveness_Scores_Cloud**
        col_index = deck_names.index(deck_name) + 3  # +3 because first two columns are ignored
        sheet2.delete_columns(col_index)

        return redirect(url_for("home"))  # Redirect to home after deletion

    # Enhanced HTML Template
    remove_deck_html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Remove Deck</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <script>
            function confirmDeletion() {
                let selectedDeck = document.getElementById("deck_name").value;
                return confirm("Are you sure you want to remove '" + selectedDeck + "'? This action cannot be undone.");
            }
        </script>
        <style>
            body {
                background-color: #f8f9fa;
                font-family: Arial, sans-serif;
                padding: 20px;
            }
            .container {
                max-width: 500px;
                margin: auto;
                padding: 20px;
                background: white;
                border-radius: 10px;
                box-shadow: 0px 4px 8px rgba(0, 0, 0, 0.2);
            }
            .btn-danger {
                width: 100%;
                margin-top: 15px;
            }
            .form-group {
                margin-bottom: 15px;
            }
            h1 {
                text-align: center;
                margin-bottom: 20px;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Remove a Deck</h1>
            <form action="{{ url_for('remove_deck') }}" method="post" onsubmit="return confirmDeletion()">
                <div class="form-group">
                    <label for="deck_name" class="form-label">Select Deck to Remove:</label>
                    <select class="form-select" id="deck_name" name="deck_name" required>
                        {% for deck in deck_names %}
                            <option value="{{ deck }}">{{ deck }}</option>
                        {% endfor %}
                    </select>
                </div>

                <button type="submit" class="btn btn-danger">Remove Deck</button>
                <a href="{{ url_for('home') }}" class="btn btn-secondary mt-2">Back to Home</a>
            </form>
        </div>

        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    </body>
    </html>
    """

    return render_template_string(remove_deck_html, deck_names=deck_names)

@app.route("/remove_card", methods=["GET", "POST"])
def remove_card():
    global effectiveness_scores_file_path

    # Load the latest version of Effectiveness_Scores_Cloud
    update_data()

    sheet = effectiveness_scores_file_path.sheet1  # First sheet in Google Sheets

    # Read data from Google Sheets
    all_data = sheet.get_all_values()
    header = all_data[0] if all_data else []  # Extract column names
    data_rows = all_data[1:]  # All rows excluding headers

    # Get the list of card names (first column)
    card_names = [row[0] for row in data_rows]

    if request.method == "POST":
        card_name = request.form.get("card_name")  # Get the selected card

        # Check if the card exists
        if card_name not in card_names:
            return f"<h1>Error</h1><p>Card '{card_name}' not found in Effectiveness_Scores_Cloud.</p>"

        # Find the row index of the card
        row_index = card_names.index(card_name) + 2  # +2 because of 1-based index and header

        # Delete the row in Google Sheets
        sheet.delete_rows(row_index)

        return redirect(url_for("home"))  # Redirect to home after deletion

    # Enhanced HTML Template
    remove_card_html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Remove Card</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <script>
            function confirmDeletion() {
                let selectedCard = document.getElementById("card_name").value;
                return confirm("Are you sure you want to remove '" + selectedCard + "'? This action cannot be undone.");
            }
        </script>
        <style>
            body {
                background-color: #f8f9fa;
                font-family: Arial, sans-serif;
                padding: 20px;
            }
            .container {
                max-width: 500px;
                margin: auto;
                padding: 20px;
                background: white;
                border-radius: 10px;
                box-shadow: 0px 4px 8px rgba(0, 0, 0, 0.2);
            }
            .btn-danger {
                width: 100%;
                margin-top: 15px;
            }
            .form-group {
                margin-bottom: 15px;
            }
            h1 {
                text-align: center;
                margin-bottom: 20px;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Remove a Card</h1>
            <form action="{{ url_for('remove_card') }}" method="post" onsubmit="return confirmDeletion()">
                <div class="form-group">
                    <label for="card_name" class="form-label">Select Card to Remove:</label>
                    <select class="form-select" id="card_name" name="card_name" required>
                        {% for card in card_names %}
                            <option value="{{ card }}">{{ card }}</option>
                        {% endfor %}
                    </select>
                </div>

                <button type="submit" class="btn btn-danger">Remove Card</button>
                <a href="{{ url_for('home') }}" class="btn btn-secondary mt-2">Back to Home</a>
            </form>
        </div>

        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    </body>
    </html>
    """

    return render_template_string(remove_card_html, card_names=card_names)

@app.route("/view_decks", methods=["GET", "POST"])
def view_decks():
    global matchup_data_file_path

    # Load the latest version of Matchup_Data_Cloud
    update_data()

    sheet = matchup_data_file_path.sheet1  # Get the first sheet

    # Read data from Google Sheets
    all_data = sheet.get_all_values()
    header = all_data[0] if all_data else []  # Extract column names
    data_rows = all_data[1:]  # All rows excluding headers

    # Extract deck names
    deck_names = [row[0] for row in data_rows] if data_rows else []

    if request.method == "POST":
        # Handle deck editing form submission
        deck_name = request.form.get("deck_name")
        new_mtgo_pr = float(request.form.get("new_mtgo_pr"))
        new_max_slots = int(request.form.get("new_max_slots"))

        # Ensure the deck exists
        if deck_name not in deck_names:
            return f"<h1>Error</h1><p>Deck '{deck_name}' not found in Matchup_Data_Cloud.</p>"

        # Find the row index of the deck
        row_index = deck_names.index(deck_name) + 2  # +2 to adjust for 1-based indexing and header row

        # Column indices (1-based index for Google Sheets)
        col_mtgo_pr = header.index("MTGO PR") + 1
        col_max_slots = header.index("Max Slots") + 1

        # Update the deck's values in Google Sheets
        sheet.update_cell(row_index, col_mtgo_pr, new_mtgo_pr)
        sheet.update_cell(row_index, col_max_slots, new_max_slots)

        return redirect(url_for("view_decks"))  # Refresh the page after updating

    # Convert the deck data to an HTML table
    deck_table = "<table class='table table-striped table-hover'><thead><tr>"
    deck_table += "".join(f"<th>{col}</th>" for col in header)
    deck_table += "</tr></thead><tbody>"
    for row in data_rows:
        deck_table += "<tr>" + "".join(f"<td>{col}</td>" for col in row) + "</tr>"
    deck_table += "</tbody></table>"

    # Enhanced HTML Template
    view_decks_html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>View & Edit Decks</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body {
                background-color: #f8f9fa;
                font-family: Arial, sans-serif;
                padding: 20px;
            }
            .container {
                max-width: 800px;
                margin: auto;
                padding: 20px;
                background: white;
                border-radius: 10px;
                box-shadow: 0px 4px 8px rgba(0, 0, 0, 0.2);
            }
            .btn-primary, .btn-secondary {
                width: 100%;
                margin-top: 10px;
            }
            h1, h2 {
                text-align: center;
                margin-bottom: 20px;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Current Decks</h1>
            <div class="table-responsive">
                {{ deck_table | safe }}
            </div>

            <h2>Edit a Deck</h2>
            <form action="{{ url_for('view_decks') }}" method="post">
                <div class="form-group">
                    <label for="deck_name" class="form-label">Select Deck:</label>
                    <select class="form-select" id="deck_name" name="deck_name" required>
                        {% for deck in deck_names %}
                            <option value="{{ deck }}">{{ deck }}</option>
                        {% endfor %}
                    </select>
                </div>

                <div class="form-group">
                    <label for="new_mtgo_pr" class="form-label">New MTGO PR (Win Rate Estimation):</label>
                    <input type="number" class="form-control" id="new_mtgo_pr" name="new_mtgo_pr" step="0.01" required>
                </div>

                <div class="form-group">
                    <label for="new_max_slots" class="form-label">New Max Sideboard Slots:</label>
                    <input type="number" class="form-control" id="new_max_slots" name="new_max_slots" required>
                </div>

                <button type="submit" class="btn btn-primary">Update Deck</button>
                <a href="{{ url_for('home') }}" class="btn btn-secondary">Back to Home</a>
            </form>
        </div>

        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    </body>
    </html>
    """

    return render_template_string(view_decks_html, deck_table=deck_table, deck_names=deck_names)

@app.route("/view_cards", methods=["GET", "POST"])
def view_cards():
    global effectiveness_scores_file_path

    # Load the latest version of Effectiveness_Scores_Cloud
    update_data()

    sheet = effectiveness_scores_file_path.sheet1  # Get the first sheet

    # Read data from Google Sheets
    all_data = sheet.get_all_values()
    header = all_data[0] if all_data else []  # Extract column names
    data_rows = all_data[1:]  # All rows excluding headers

    # Extract card names (first column)
    card_names = [row[0] for row in data_rows] if data_rows else []
    deck_names = header[2:] if len(header) > 2 else []  # Skip first two columns

    if request.method == "POST":
        # Handle card editing form submission
        card_name = request.form.get("card_name")
        new_max_copies = int(request.form.get("new_max_copies"))

        # Ensure the card exists
        if card_name not in card_names:
            return f"<h1>Error</h1><p>Card '{card_name}' not found in Effectiveness_Scores_Cloud.</p>"

        # Find the row index of the card
        row_index = card_names.index(card_name) + 2  # +2 to adjust for 1-based indexing and header row

        # Column index for "Max Copies"
        col_max_copies = header.index("Max Copies") + 1

        # Update max copies in Google Sheets
        sheet.update_cell(row_index, col_max_copies, new_max_copies)

        # Update effectiveness scores
        updates = []
        for deck in deck_names:
            new_score = int(request.form.get(f"effectiveness[{deck}]"))
            col_index = header.index(deck) + 1  # Get column index for the deck
            updates.append({
                "range": f"{chr(64 + col_index)}{row_index}",  # Convert index to Google Sheets column letter
                "values": [[new_score]]
            })

        # Perform batch update
        sheet.batch_update(updates)

        return redirect(url_for("view_cards"))  # Refresh the page after updating

    # Convert the card data to an HTML table
    card_table = "<table class='table table-striped table-hover'><thead><tr>"
    card_table += "".join(f"<th>{col}</th>" for col in header)
    card_table += "</tr></thead><tbody>"
    for row in data_rows:
        card_table += "<tr>" + "".join(f"<td>{col}</td>" for col in row) + "</tr>"
    card_table += "</tbody></table>"

    # Enhanced HTML Template
    view_cards_html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>View & Edit Cards</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body {
                background-color: #f8f9fa;
                font-family: Arial, sans-serif;
                padding: 20px;
            }
            .container {
                max-width: 800px;
                margin: auto;
                padding: 20px;
                background: white;
                border-radius: 10px;
                box-shadow: 0px 4px 8px rgba(0, 0, 0, 0.2);
            }
            .btn-primary, .btn-secondary {
                width: 100%;
                margin-top: 10px;
            }
            .table-responsive {
                margin-bottom: 20px;
            }
            h1, h2 {
                text-align: center;
                margin-bottom: 20px;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Current Sideboard Cards</h1>
            <div class="table-responsive">
                {{ card_table | safe }}
            </div>

            <h2>Edit a Card</h2>
            <form action="{{ url_for('view_cards') }}" method="post">
                <div class="form-group">
                    <label for="card_name" class="form-label">Select Card:</label>
                    <select class="form-select" id="card_name" name="card_name" required>
                        {% for card in card_names %}
                            <option value="{{ card }}">{{ card }}</option>
                        {% endfor %}
                    </select>
                </div>

                <div class="form-group">
                    <label for="new_max_copies" class="form-label">New Max Copies:</label>
                    <input type="number" class="form-control" id="new_max_copies" name="new_max_copies" min="0" required>
                </div>

                <h3 class="mt-3">Update Effectiveness Scores (1-10):</h3>
                {% for deck in deck_names %}
                <div class="form-group">
                    <label for="{{ deck }}" class="form-label">{{ deck }}:</label>
                    <input type="number" class="form-control" id="{{ deck }}" name="effectiveness[{{ deck }}]" min="0" max="10" required>
                </div>
                {% endfor %}

                <button type="submit" class="btn btn-primary">Update Card</button>
                <a href="{{ url_for('home') }}" class="btn btn-secondary">Back to Home</a>
            </form>
        </div>

        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    </body>
    </html>
    """

    return render_template_string(view_cards_html, 
                                  card_table=card_table, 
                                  card_names=card_names,
                                  deck_names=deck_names)

@app.route("/sideboard")
def run_sideboard_optimizer():
    try:
        update_data()
        sideboard_map = assign_sideboard_cards(15)
        sideboard_map = refine_sideboard(sideboard_map)

        # Convert sideboard results into an HTML table
        sideboard_table = """
        <table class="table table-striped table-hover">
            <thead class="thead-dark">
                <tr>
                    <th>Card</th>
                    <th>Quantity</th>
                </tr>
            </thead>
            <tbody>
        """
        for card, quantity in sideboard_map.items():
            sideboard_table += f"<tr><td>{card}</td><td>{quantity}</td></tr>"
        sideboard_table += "</tbody></table>"

        # Bootstrap-enhanced HTML
        sideboard_html = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Sideboard Optimizer</title>
            <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
            <style>
                body {{
                    background-color: #f8f9fa;
                    font-family: Arial, sans-serif;
                    padding: 20px;
                }}
                .container {{
                    max-width: 600px;
                    margin: auto;
                    padding: 20px;
                    background: white;
                    border-radius: 10px;
                    box-shadow: 0px 4px 8px rgba(0, 0, 0, 0.2);
                    text-align: center;
                }}
                h1 {{
                    text-align: center;
                    margin-bottom: 20px;
                }}
                .btn-primary, .btn-secondary {{
                    width: 100%;
                    margin-top: 10px;
                }}
                .table {{
                    margin-top: 15px;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>Optimized Sideboard</h1>
                {sideboard_table}
                <a href="{{{{ url_for('home') }}}}" class="btn btn-secondary">Back to Home</a>
            </div>

            <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
        </body>
        </html>
        """

        return render_template_string(sideboard_html)

    except Exception as e:
        error_html = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Error</title>
            <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        </head>
        <body>
            <div class="container mt-5">
                <div class="alert alert-danger text-center" role="alert">
                    ❌ Error running program: {e}
                </div>
                <a href="{{{{ url_for('home') }}}}" class="btn btn-secondary">Back to Home</a>
            </div>
        </body>
        </html>
        """
        return render_template_string(error_html)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

