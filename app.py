from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
import boto3
from boto3.dynamodb.conditions import Key, Attr
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from decimal import Decimal
import uuid
import random

app = Flask(__name__)
app.secret_key = 'your_secret_key_here' # IMPORTANT: Change this to a strong, random key in production!

# AWS Setup using IAM Role
REGION = 'ap-south-1'  # Replace with your actual AWS region
dynamodb = boto3.resource('dynamodb', region_name=REGION)
sns_client = boto3.client('sns', region_name=REGION)

users_table = dynamodb.Table('travelgo_users')
trains_table = dynamodb.Table('trains') # Note: This table is declared but not used in the provided routes.
bookings_table = dynamodb.Table('bookings')

SNS_TOPIC_ARN = 'arn:aws:sns:ap-south-1:353250843450:TravelGoBookingTopic'  # Replace with actual SNS topic ARN

# Function to send SNS notifications
# This function is duplicated in the original code, removing the duplicate.
def send_sns_notification(subject, message):
    try:
        sns_client.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject,
            Message=message
        )
    except Exception as e:
        print(f"SNS Error: Could not send notification - {e}")
        # Optionally, flash an error message to the user or log it more robustly.

# Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        
        # Check if user already exists
        # This uses get_item on the primary key 'email', so no GSI needed.
        existing = users_table.get_item(Key={'email': email})
        if 'Item' in existing:
            flash('Email already exists!', 'error')
            return render_template('register.html')
        
        # Hash password and store user
        hashed_password = generate_password_hash(password)
        users_table.put_item(Item={'email': email, 'password': hashed_password})
        flash('Registration successful! Please log in.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        
        # Retrieve user by email (primary key)
        user = users_table.get_item(Key={'email': email})
        
        # Authenticate user
        if 'Item' in user and check_password_hash(user['Item']['password'], password):
            session['email'] = email
            flash('Logged in successfully!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid email or password!', 'error')
            return render_template('login.html')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('email', None)
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))

@app.route('/dashboard')
def dashboard():
    if 'email' not in session:
        return redirect(url_for('login'))
    user_email = session['email']
    
    # Query bookings for the logged-in user using the primary key 'user_email'
    # No GSI is needed here as 'user_email' is likely the partition key for the bookings_table.
    response = bookings_table.query(
        KeyConditionExpression=Key('user_email').eq(user_email),
        ScanIndexForward=False # Get most recent bookings first
    )
    bookings = response.get('Items', [])
    
    # Convert Decimal types from DynamoDB to float for display if necessary
    for booking in bookings:
        if 'total_price' in booking:
            try:
                booking['total_price'] = float(booking['total_price'])
            except (TypeError, ValueError):
                booking['total_price'] = 0.0 # Default value if conversion fails
    return render_template('dashboard.html', username=user_email, bookings=bookings)

@app.route('/train')
def train():
    if 'email' not in session:
        return redirect(url_for('login'))
    return render_template('train.html')

@app.route('/confirm_train_details')
def confirm_train_details():
    if 'email' not in session:
        return redirect(url_for('login'))

    booking_details = {
        'name': request.args.get('name'),
        'train_number': request.args.get('trainNumber'),
        'source': request.args.get('source'),
        'destination': request.args.get('destination'),
        'departure_time': request.args.get('departureTime'),
        'arrival_time': request.args.get('arrivalTime'),
        'price_per_person': Decimal(request.args.get('price')),
        'travel_date': request.args.get('date'),
        'num_persons': int(request.args.get('persons')),
        'item_id': request.args.get('trainId'), # This is the train ID
        'booking_type': 'train',
        'user_email': session['email'],
        'total_price': Decimal(request.args.get('price')) * int(request.args.get('persons'))
    }

    # IMPORTANT CHANGE: Replaced GSI query with a scan operation and FilterExpression
    # This will scan the entire table and then filter, which can be inefficient for large tables.
    response = bookings_table.scan(
        FilterExpression=Attr('item_id').eq(booking_details['item_id']) &
                         Attr('travel_date').eq(booking_details['travel_date']) &
                         Attr('booking_type').eq('train') # Ensure we're only looking at train bookings for seat allocation
    )

    booked_seats = set()
    for b in response.get('Items', []):
        if 'seats_display' in b:
            booked_seats.update(b['seats_display'].split(', '))

    all_seats = [f"S{i}" for i in range(1, 101)] # Assuming 100 seats per train
    available_seats = [seat for seat in all_seats if seat not in booked_seats]

    if len(available_seats) < booking_details['num_persons']:
        flash("Not enough seats available. Please try fewer persons or a different train/date.", "error")
        return redirect(url_for("train"))

    # Allocate random seats for display on the confirmation page
    # These are potential seats, not yet confirmed.
    seats_for_display = random.sample(available_seats, booking_details['num_persons'])
    
    # Store the full booking details including the proposed seats for the final confirmation
    booking_details['proposed_seats_display'] = ', '.join(seats_for_display)
    session['pending_booking'] = booking_details
    
    return render_template('confirm_train_details.html', booking=booking_details, available_seats_display=seats_for_display)

@app.route('/final_confirm_train_booking', methods=['POST'])
def final_confirm_train_booking():
    if 'email' not in session:
        return jsonify({'success': False, 'message': 'User not logged in'}), 401

    booking_data = session.pop('pending_booking', None)
    if not booking_data:
        return jsonify({'success': False, 'message': 'No pending booking found or session expired'}), 400
    
    # IMPORTANT CHANGE: Re-checking seat availability with a scan for consistency
    response = bookings_table.scan(
        FilterExpression=Attr('item_id').eq(booking_data['item_id']) &
                         Attr('travel_date').eq(booking_data['travel_date']) &
                         Attr('booking_type').eq('train')
    )

    booked_seats_current = set()
    for b in response.get('Items', []):
        if 'seats_display' in b:
            booked_seats_current.update(b['seats_display'].split(', '))

    all_seats = [f"S{i}" for i in range(1, 101)]
    available_seats_current = [seat for seat in all_seats if seat not in booked_seats_current]

    if len(available_seats_current) < booking_data['num_persons']:
        return jsonify({'success': False, 'message': 'Not enough seats available. Another booking might have taken them.'}), 400

    # Re-allocate seats based on current availability, as the previously proposed ones might be taken
    allocated_seats = random.sample(available_seats_current, booking_data['num_persons'])
    booking_data['seats_display'] = ', '.join(allocated_seats)
    
    booking_data['booking_id'] = str(uuid.uuid4())
    booking_data['booking_date'] = datetime.now().isoformat() # Use ISO format for consistent storage

    # Store the booking in DynamoDB
    try:
        bookings_table.put_item(Item=booking_data)
    except Exception as e:
        print(f"DynamoDB Error: {e}")
        return jsonify({'success': False, 'message': f'Failed to confirm booking due to database error: {e}'}), 500

    # Send SNS notification
    send_sns_notification(
        subject="Train Booking Confirmed",
        message=f"Dear {booking_data['user_email']},\nYour train booking for {booking_data['name']} (Train No: {booking_data['train_number']}) from {booking_data['source']} to {booking_data['destination']} on {booking_data['travel_date']} is confirmed.\nYour allocated seats are: {booking_data['seats_display']}\nTotal Price: ₹{booking_data['total_price']}"
    )

    return jsonify({'success': True, 'message': 'Train booking confirmed successfully!', 'redirect': url_for('dashboard')})

@app.route('/bus')
def bus():
    if 'email' not in session:
        return redirect(url_for('login'))
    return render_template('bus.html')

@app.route('/confirm_bus_details')
def confirm_bus_details():
    if 'email' not in session:
        return redirect(url_for('login'))

    booking_details = {
        'name': request.args.get('name'),
        'source': request.args.get('source'),
        'destination': request.args.get('destination'),
        'time': request.args.get('time'),
        'type': request.args.get('type'),
        'price_per_person': Decimal(request.args.get('price')),
        'travel_date': request.args.get('date'),
        'num_persons': int(request.args.get('persons')),
        'item_id': request.args.get('busId'), # This is the bus ID
        'booking_type': 'bus',
        'user_email': session['email'],
        'total_price': Decimal(request.args.get('price')) * int(request.args.get('persons'))
    }
    
    # Store pending booking for seat selection
    session['pending_booking'] = booking_details
    # Redirect to seat selection page
    return redirect(url_for('select_bus_seats',
                            name=booking_details['name'],
                            source=booking_details['source'],
                            destination=booking_details['destination'],
                            time=booking_details['time'],
                            type=booking_details['type'],
                            price=str(booking_details['price_per_person']), # Pass as string
                            date=booking_details['travel_date'],
                            persons=str(booking_details['num_persons']), # Pass as string
                            busId=booking_details['item_id']))


@app.route('/select_bus_seats')
def select_bus_seats():
    if 'email' not in session:
        return redirect(url_for('login'))

    # Retrieve booking details from session or URL parameters if not in session
    # Prioritize session if available, as it holds the 'pending_booking' from previous step
    booking = session.get('pending_booking', {})
    if not booking: # If not in session, reconstruct from query parameters
        booking = {
            'name': request.args.get('name'),
            'source': request.args.get('source'),
            'destination': request.args.get('destination'),
            'time': request.args.get('time'),
            'type': request.args.get('type'),
            'price_per_person': Decimal(request.args.get('price')),
            'travel_date': request.args.get('date'),
            'num_persons': int(request.args.get('persons')),
            'item_id': request.args.get('busId'),
            'booking_type': 'bus',
            'user_email': session['email'],
            'total_price': Decimal(request.args.get('price')) * int(request.args.get('persons'))
        }
        session['pending_booking'] = booking # Store if reconstructed

    # IMPORTANT CHANGE: Replaced GSI query with a scan operation and FilterExpression
    response = bookings_table.scan(
        FilterExpression=Attr('item_id').eq(booking['item_id']) &
                         Attr('travel_date').eq(booking['travel_date']) &
                         Attr('booking_type').eq('bus') # Ensure we're only looking at bus bookings
    )

    booked_seats = set()
    for b in response.get('Items', []):
        if 'seats_display' in b:
            booked_seats.update(b['seats_display'].split(', '))

    all_seats = [f"S{i}" for i in range(1, 41)] # Assuming 40 seats per bus

    return render_template("select_bus_seats.html", booking=booking, booked_seats=booked_seats, all_seats=all_seats)

@app.route('/final_confirm_bus_booking', methods=['POST'])
def final_confirm_bus_booking():
    if 'email' not in session:
        return redirect(url_for('login'))

    booking = session.pop('pending_booking', None)
    selected_seats_str = request.form.get('selected_seats') # Get as string
    
    if not booking or not selected_seats_str:
        flash("Booking failed! Missing data or session expired.", "error")
        return redirect(url_for("bus"))

    selected_seats = selected_seats_str.split(', ')

    # IMPORTANT CHANGE: Re-checking seat availability with a scan to prevent double booking
    response = bookings_table.scan(
        FilterExpression=Attr('item_id').eq(booking['item_id']) &
                         Attr('travel_date').eq(booking['travel_date']) &
                         Attr('booking_type').eq('bus')
    )
    existing_booked_seats = set()
    for b in response.get('Items', []):
        if 'seats_display' in b:
            existing_booked_seats.update(b['seats_display'].split(', '))

    # Check if any of the selected seats are already booked
    if any(s in existing_booked_seats for s in selected_seats):
        flash("One or more selected seats are already booked by another user. Please choose again.", "error")
        # Return to seat selection with current available seats
        booking['seats_display'] = ', '.join(selected_seats) # Keep selected seats for re-display
        session['pending_booking'] = booking # Put back into session
        return redirect(url_for('select_bus_seats', 
                                name=booking['name'],
                                source=booking['source'],
                                destination=booking['destination'],
                                time=booking['time'],
                                type=booking['type'],
                                price=str(booking['price_per_person']),
                                date=booking['travel_date'],
                                persons=str(booking['num_persons']),
                                busId=booking['item_id']))

    # All checks passed, finalize booking
    booking['seats_display'] = selected_seats_str # Store as comma-separated string
    booking['booking_id'] = str(uuid.uuid4())
    booking['booking_date'] = datetime.now().isoformat()

    try:
        bookings_table.put_item(Item=booking)
    except Exception as e:
        print(f"DynamoDB Error: {e}")
        flash(f"Failed to confirm bus booking due to database error: {e}", 'error')
        return redirect(url_for("bus"))
    
    send_sns_notification(
        subject="Bus Booking Confirmed",
        message=f"Dear {booking['user_email']},\nYour bus from {booking['source']} to {booking['destination']} on {booking['travel_date']} at {booking['time']} ({booking['type']}) is confirmed.\nYour selected seats are: {booking['seats_display']}\nTotal Price: ₹{booking['total_price']}"
    )

    flash('Bus booking confirmed successfully!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/flight')
def flight():
    if 'email' not in session:
        return redirect(url_for('login'))
    return render_template('flight.html')

@app.route('/confirm_flight_details')
def confirm_flight_details():
    if 'email' not in session:
        return redirect(url_for('login')) # Ensure user is logged in
        
    booking = {
        'flight_id': request.args['flight_id'],
        'airline': request.args['airline'],
        'flight_number': request.args['flight_number'],
        'source': request.args['source'],
        'destination': request.args['destination'],
        'departure_time': request.args['departure'],
        'arrival_time': request.args['arrival'],
        'travel_date': request.args['date'],
        'num_persons': int(request.args['passengers']),
        'price_per_person': Decimal(request.args['price']), # Convert to Decimal for consistency
    }
    booking['total_price'] = booking['price_per_person'] * booking['num_persons']
    session['pending_booking'] = booking # Store for final confirmation
    return render_template('confirm_flight_details.html', booking=booking)

@app.route('/confirm_flight_booking', methods=['POST'])
def confirm_flight_booking():
    if 'email' not in session:
        return redirect(url_for('login'))

    booking = session.pop('pending_booking', None)
    if not booking:
        flash("Flight booking failed! No pending booking found or session expired.", "error")
        return redirect(url_for('flight'))
        
    # The form submission might have redundant data, use the session's pending booking
    # and just add booking_date, booking_id, and user_email
    booking['user_email'] = session['email']
    booking['booking_date'] = datetime.now().isoformat()
    booking['booking_id'] = str(uuid.uuid4())
    booking['booking_type'] = 'flight' # Explicitly set booking type

    try:
        bookings_table.put_item(Item=booking)
    except Exception as e:
        print(f"DynamoDB Error: {e}")
        flash(f"Failed to confirm flight booking due to database error: {e}", 'error')
        return redirect(url_for("flight"))

    send_sns_notification(
        subject="Flight Booking Confirmed",
        message=f"Dear {booking['user_email']},\nYour flight booking on {booking['travel_date']} from {booking['source']} to {booking['destination']} with {booking['airline']} (Flight No: {booking['flight_number']}) is confirmed.\nTotal Price: ₹{booking['total_price']}"
    )

    flash('Flight booking confirmed successfully!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/hotel')
def hotel():
    if 'email' not in session:
        return redirect(url_for('login'))
    return render_template('hotel.html')

@app.route('/confirm_hotel_details')
def confirm_hotel_details():
    if 'email' not in session:
        return redirect(url_for('login'))

    booking = {
        'name': request.args.get('name'),
        'location': request.args.get('location'),
        'checkin_date': request.args.get('checkin'),
        'checkout_date': request.args.get('checkout'),
        'num_rooms': int(request.args.get('rooms')),
        'num_guests': int(request.args.get('guests')),
        'price_per_night': Decimal(request.args.get('price')), # Convert to Decimal
        'rating': int(request.args.get('rating'))
    }

    try:
        ci = datetime.fromisoformat(booking['checkin_date'])
        co = datetime.fromisoformat(booking['checkout_date'])
        nights = (co - ci).days
        if nights < 0:
            flash("Checkout date cannot be before check-in date.", "error")
            return redirect(url_for('hotel'))
        elif nights == 0:
            flash("Check-in and check-out dates cannot be the same for a hotel booking.", "error")
            return redirect(url_for('hotel'))
        booking['nights'] = nights
        booking['total_price'] = booking['price_per_night'] * booking['num_rooms'] * nights
    except ValueError:
        flash("Invalid date format provided.", "error")
        return redirect(url_for('hotel'))

    session['pending_booking'] = booking # Store for final confirmation
    return render_template('confirm_hotel_details.html', booking=booking)

@app.route('/confirm_hotel_booking', methods=['POST'])
def confirm_hotel_booking():
    if 'email' not in session:
        return redirect(url_for('login'))

    booking = session.pop('pending_booking', None)
    if not booking:
        flash("Hotel booking failed! No pending booking found or session expired.", "error")
        return redirect(url_for('hotel'))

    # The form submission might have redundant data, use the session's pending booking
    # and just add booking_date, booking_id, and user_email
    booking['user_email'] = session['email']
    booking['booking_date'] = datetime.now().isoformat()
    booking['booking_id'] = str(uuid.uuid4())
    booking['booking_type'] = 'hotel' # Explicitly set booking type

    try:
        bookings_table.put_item(Item=booking)
    except Exception as e:
        print(f"DynamoDB Error: {e}")
        flash(f"Failed to confirm hotel booking due to database error: {e}", 'error')
        return redirect(url_for("hotel"))

    send_sns_notification(
        subject="Hotel Booking Confirmed",
        message=f"Dear {booking['user_email']},\nHotel booking at {booking['name']} in {booking['location']} from {booking['checkin_date']} to {booking['checkout_date']} for {booking['num_rooms']} rooms and {booking['num_guests']} guests is confirmed.\nTotal Price: ₹{booking['total_price']}"
    )

    flash('Hotel booking confirmed successfully!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/cancel_booking', methods=['POST'])
def cancel_booking():
    if 'email' not in session:
        return redirect(url_for('login'))

    booking_id = request.form.get('booking_id')
    user_email = session['email']
    booking_date = request.form.get('booking_date') # This is crucial as it's the sort key

    if not booking_id or not booking_date:
        flash("Error: Booking ID or Booking Date is missing for cancellation.", 'error')
        return redirect(url_for('dashboard'))

    try:
        # Delete item using the primary key (user_email and booking_date)
        # This does not use GSI, so it remains unchanged.
        bookings_table.delete_item(
            Key={'user_email': user_email, 'booking_date': booking_date}
        )
        flash(f"Booking {booking_id} cancelled successfully!", 'success')
    except Exception as e:
        flash(f"Failed to cancel booking {booking_id}: {str(e)}", 'error')

    return redirect(url_for('dashboard'))


if __name__ == '__main__':
    # IMPORTANT: In a production environment, disable debug mode and specify a production-ready host.
    app.run(debug=True, host='0.0.0.0')

