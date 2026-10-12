import { useState, useEffect, useCallback, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import type { Booking, Flight, StoredHold, ErrorResponse, WaitlistEntry } from '../types';
import { LoadingSpinner, Modal, Button } from '../components/common';
import { BookingCard } from '../components/bookings/BookingCard';
import { HoldCard } from '../components/bookings/HoldCard';
import { WaitlistCard } from '../components/bookings/WaitlistCard';
import { getUserBookings, getFlights, cancelBooking, getHold, isErrorResponse, getUserWaitlist, leaveWaitlist } from '../services/api';
import { getStoredHolds, removeHold } from '../utils/holdStorage';
import { useUser } from '../hooks/useUserContext';
import { AlertCircle } from 'lucide-react';
import toast from 'react-hot-toast';
import { motion } from 'framer-motion';

// ---------------------------------------------------------------------------
// Promotion tracking helpers (localStorage, keyed by user_id)
// Bookings created via the waitlist are identified by cross-referencing
// waitlist entries with status='booked'.  A "seen" set in localStorage
// prevents the toast from firing again on subsequent page loads.
// ---------------------------------------------------------------------------

const SEEN_KEY = (userId: number) => `galaxium_seen_promotions_${userId}`;

function getSeenPromotions(userId: number): Set<number> {
  try {
    const raw = localStorage.getItem(SEEN_KEY(userId));
    if (!raw) return new Set();
    return new Set(JSON.parse(raw) as number[]);
  } catch {
    return new Set();
  }
}

function markPromotionsSeen(userId: number, bookingIds: number[]): void {
  try {
    const existing = getSeenPromotions(userId);
    bookingIds.forEach((id) => existing.add(id));
    localStorage.setItem(SEEN_KEY(userId), JSON.stringify([...existing]));
  } catch {
    // localStorage unavailable — silently ignore
  }
}

export const MyBookings = () => {
  const { user } = useUser();
  const navigate = useNavigate();
  const [bookings, setBookings] = useState<Booking[]>([]);
  const [flights, setFlights] = useState<Flight[]>([]);
  const [activeHolds, setActiveHolds] = useState<StoredHold[]>([]);
  const [waitlistEntries, setWaitlistEntries] = useState<WaitlistEntry[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [cancellingId, setCancellingId] = useState<number | null>(null);
  const [showCancelModal, setShowCancelModal] = useState(false);
  const [bookingToCancel, setBookingToCancel] = useState<number | null>(null);
  const [leavingWaitlistId, setLeavingWaitlistId] = useState<number | null>(null);
  // Set of booking_ids that came from the waitlist and haven't been shown a toast yet
  const [newPromotionIds, setNewPromotionIds] = useState<Set<number>>(new Set());
  // Ref so loadData can read the latest value without re-running the effect
  const newPromotionIdsRef = useRef<Set<number>>(new Set());

  const loadHolds = useCallback(async () => {
    if (!user) return;

    const stored = getStoredHolds(user.user_id);
    if (stored.length === 0) {
      setActiveHolds([]);
      return;
    }

    // Verify each hold's current status from the API, remove stale ones
    const stillActive: StoredHold[] = [];
    const isLocallyExpired = (sh: StoredHold) => {
      const expiryTime = new Date(sh.reservedUntil).getTime();
      return isNaN(expiryTime) || expiryTime < Date.now();
    };
    await Promise.all(
      stored.map(async (sh) => {
        try {
          const hold = await getHold(sh.holdId);
          if (hold.status === 'HELD' && !isLocallyExpired(sh)) {
            stillActive.push(sh);
          } else {
            // Hold is no longer active (confirmed, released, expired, or locally timed out)
            removeHold(user.user_id, sh.holdId);
          }
        } catch {
          // API unavailable — fall back to local expiry check
          if (!isLocallyExpired(sh)) {
            stillActive.push(sh);
          } else {
            removeHold(user.user_id, sh.holdId);
          }
        }
      })
    );

    setActiveHolds(stillActive);
  }, [user]);

  const loadData = useCallback(async () => {
    if (!user) return;

    setIsLoading(true);
    try {
      const [bookingsData, flightsData, waitlistData] = await Promise.all([
        getUserBookings(user.user_id),
        getFlights(),
        getUserWaitlist(user.user_id),
      ]);
      setBookings(bookingsData);
      setFlights(flightsData);
      setWaitlistEntries(waitlistData);
      await loadHolds();

      // --- Promotion detection ---
      // waitlist entries with status='booked' each correspond to a booking
      // that was created via the waitlist promotion path.  We cross-reference
      // those against the booking list and identify any that the user hasn't
      // seen a toast for yet.
      // Map of "flight_id:seat_class" → added_at (ISO string) for booked waitlist entries.
      const bookedWaitlistAddedAt = new Map<string, string>(
        waitlistData
          .filter((e) => e.status === 'booked')
          .map((e) => [`${e.flight_id}:${e.seat_class}`, e.added_at])
      );

      const seen = getSeenPromotions(user.user_id);
      const fresh: number[] = [];
      for (const b of bookingsData) {
        const waitlistAddedAt = bookedWaitlistAddedAt.get(`${b.flight_id}:${b.seat_class}`);
        if (
          b.status === 'booked' &&
          !seen.has(b.booking_id) &&
          waitlistAddedAt !== undefined &&
          b.booking_time > waitlistAddedAt
        ) {
          fresh.push(b.booking_id);
        }
      }

      if (fresh.length > 0) {
        const updated = new Set([...newPromotionIdsRef.current, ...fresh]);
        newPromotionIdsRef.current = updated;
        setNewPromotionIds(new Set(updated));
        markPromotionsSeen(user.user_id, fresh);
        toast.success('🎉 You got a seat from the waitlist!', { duration: 5000 });
      }
    } catch (err) {
      toast.error('Failed to load bookings');
      console.error(err);
    } finally {
      setIsLoading(false);
    }
  }, [user, loadHolds]);

  useEffect(() => {
    if (!user) {
      navigate('/flights');
      return;
    }
    loadData();
  }, [user, navigate, loadData]);

  const handleCancelClick = (bookingId: number) => {
    setBookingToCancel(bookingId);
    setShowCancelModal(true);
  };

  const handleLeaveWaitlist = async (waitlistId: number) => {
    if (!user) return;
    setLeavingWaitlistId(waitlistId);
    try {
      const result = await leaveWaitlist(waitlistId, user.user_id);
      if (isErrorResponse(result)) {
        toast.error(result.details ?? result.error);
        return;
      }
      toast.success('Removed from waitlist');
      loadData();
    } catch (err) {
      const error = err as ErrorResponse;
      toast.error(error.details ?? error.error ?? 'Failed to leave waitlist');
    } finally {
      setLeavingWaitlistId(null);
    }
  };

  const handleConfirmCancel = async () => {
    if (!bookingToCancel) return;

    setCancellingId(bookingToCancel);
    setShowCancelModal(false);

    try {
      const result = await cancelBooking(bookingToCancel);

      if (isErrorResponse(result)) {
        toast.error(result.details || result.error);
        return;
      }

      toast.success('Booking cancelled successfully');
      loadData();
    } catch (err) {
      const error = err as ErrorResponse;
      toast.error(error.details || error.error || 'Failed to cancel booking');
    } finally {
      setCancellingId(null);
      setBookingToCancel(null);
    }
  };

  const getFlightForBooking = (booking: Booking): Flight | undefined => {
    return flights.find((f) => f.flight_id === booking.flight_id);
  };

  const getFlightForHold = (hold: StoredHold): Flight | undefined => {
    return flights.find((f) => f.flight_id === hold.flightId);
  };

  const getFlightForWaitlist = (entry: WaitlistEntry): Flight | undefined => {
    return flights.find((f) => f.flight_id === entry.flight_id);
  };

  const activeBookings = bookings.filter((b) => b.status === 'booked');
  const pastBookings = bookings.filter((b) => b.status !== 'booked');
  const waitingEntries = waitlistEntries.filter((e) => e.status === 'waiting');

  if (!user) {
    return null;
  }

  return (
    <div className="space-y-8">
      {/* Header */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        className="text-center"
      >
        <h1 className="text-4xl md:text-5xl font-bold text-star-white mb-4">
          My <span className="bg-cosmic-gradient bg-clip-text text-transparent">Bookings</span>
        </h1>
        <p className="text-star-white/70 text-lg">
          Manage your space travel reservations
        </p>
      </motion.div>

      {isLoading ? (
        <LoadingSpinner size="lg" text="Loading your bookings..." />
      ) : (
        <div className="space-y-8">
          {/* Pending Holds */}
          {activeHolds.length > 0 && (
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.05 }}
            >
              <div className="flex items-center gap-3 mb-4">
                <h2 className="text-2xl font-bold text-solar-orange">
                  Pending Holds ({activeHolds.length})
                </h2>
                <span className="text-xs text-star-white/50 bg-solar-orange/10 border border-solar-orange/30 px-2 py-1 rounded-full">
                  Confirm before time runs out
                </span>
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                {activeHolds.map((hold) => (
                  <HoldCard
                    key={hold.holdId}
                    storedHold={hold}
                    flight={getFlightForHold(hold)}
                    onAction={loadData}
                  />
                ))}
              </div>
            </motion.div>
          )}

          {/* Waitlist Entries */}
          {waitingEntries.length > 0 && (
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.07 }}
            >
              <div className="flex items-center gap-3 mb-4">
                <h2 className="text-2xl font-bold text-solar-orange">
                  Waitlist ({waitingEntries.length})
                </h2>
                <span className="text-xs text-star-white/50 bg-solar-orange/10 border border-solar-orange/30 px-2 py-1 rounded-full">
                  You'll be notified when a seat opens up
                </span>
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                {waitingEntries.map((entry) => (
                  <WaitlistCard
                    key={entry.waitlist_id}
                    entry={entry}
                    flight={getFlightForWaitlist(entry)}
                    onLeave={handleLeaveWaitlist}
                    isLeaving={leavingWaitlistId === entry.waitlist_id}
                  />
                ))}
              </div>
            </motion.div>
          )}

          {/* No content at all */}
          {bookings.length === 0 && activeHolds.length === 0 && waitingEntries.length === 0 && (
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              className="glass-card p-12 text-center"
            >
              <AlertCircle className="mx-auto mb-4 text-star-white/50" size={48} />
              <h3 className="text-xl font-semibold text-star-white mb-2">
                No bookings yet
              </h3>
              <p className="text-star-white/70 mb-6">
                Start your space adventure by booking your first flight!
              </p>
              <Button onClick={() => navigate('/flights')}>Browse Flights</Button>
            </motion.div>
          )}

          {/* Active Bookings */}
          {activeBookings.length > 0 && (
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.1 }}
            >
              <h2 className="text-2xl font-bold text-star-white mb-4">
                Active Bookings ({activeBookings.length})
              </h2>
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                {activeBookings.map((booking) => (
                  <BookingCard
                    key={booking.booking_id}
                    booking={booking}
                    flight={getFlightForBooking(booking)}
                    onCancel={handleCancelClick}
                    isCancelling={cancellingId === booking.booking_id}
                    fromWaitlist={newPromotionIds.has(booking.booking_id)}
                  />
                ))}
              </div>
            </motion.div>
          )}

          {/* Past Bookings */}
          {pastBookings.length > 0 && (
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.2 }}
            >
              <h2 className="text-2xl font-bold text-star-white mb-4">
                Past Bookings ({pastBookings.length})
              </h2>
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
                {pastBookings.map((booking) => (
                  <BookingCard
                    key={booking.booking_id}
                    booking={booking}
                    flight={getFlightForBooking(booking)}
                    onCancel={handleCancelClick}
                  />
                ))}
              </div>
            </motion.div>
          )}
        </div>
      )}

      {/* Cancel Confirmation Modal */}
      <Modal
        isOpen={showCancelModal}
        onClose={() => setShowCancelModal(false)}
        title="Cancel Booking"
        size="sm"
      >
        <div className="space-y-4">
          <p className="text-star-white/70">
            Are you sure you want to cancel this booking? This action cannot be undone.
          </p>
          <div className="flex gap-3">
            <Button
              variant="secondary"
              onClick={() => setShowCancelModal(false)}
              className="flex-1"
            >
              Keep Booking
            </Button>
            <Button variant="danger" onClick={handleConfirmCancel} className="flex-1">
              Cancel Booking
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  );
};

// Made with Bob
