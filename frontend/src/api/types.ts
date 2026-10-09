// Matches the existing FastAPI response models; timestamps stay ISO strings.
export type Role = 'passenger' | 'driver' | 'admin';
export interface User { id: number; name: string; email: string; role: Role }
export interface Token { access_token: string; token_type: string }
export interface LoginInput { email: string; password: string }
export interface RegisterInput extends LoginInput {
  name: string; cnic: string; phone_number: string; role: 'passenger' | 'driver';
}
export type RideStatus = 'active' | 'completed' | 'cancelled';
export type BookingStatus = 'pending' | 'accepted' | 'rejected' | 'cancelled';
export interface Ride {
  id: number; driver_id: number; origin: string; destination: string;
  departure_time: string; available_seats: number; fare_per_seat: number; status: RideStatus;
}
export interface PassengerBooking {
  id: number; ride_id: number; passenger_id: number; status: BookingStatus; ride: Ride;
}
