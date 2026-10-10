import type { WaitlistEntry, Flight } from '../../types';
import { Card, Button } from '../common';
import { Plane, Calendar, Clock, Crown, Rocket } from 'lucide-react';
import { formatDate } from '../../utils/formatters';
import { motion } from 'framer-motion';

interface WaitlistCardProps {
  entry: WaitlistEntry;
  flight?: Flight;
  onLeave: (waitlistId: number) => void;
  isLeaving?: boolean;
}

export const WaitlistCard = ({ entry, flight, onLeave, isLeaving }: WaitlistCardProps) => {
  const getSeatClassIcon = () => {
    switch (entry.seat_class) {
      case 'business':
        return <Crown className="text-purple-400" size={16} />;
      case 'galaxium':
        return <Rocket className="text-alien-green" size={16} />;
      default:
        return <Plane className="text-blue-400" size={16} />;
    }
  };

  const getSeatClassName = () => {
    switch (entry.seat_class) {
      case 'business':
        return 'Business';
      case 'galaxium':
        return 'Galaxium Class';
      default:
        return 'Economy';
    }
  };

  const getSeatClassColor = () => {
    switch (entry.seat_class) {
      case 'business':
        return 'text-purple-400';
      case 'galaxium':
        return 'text-alien-green';
      default:
        return 'text-blue-400';
    }
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      whileHover={{ y: -2 }}
      transition={{ duration: 0.2 }}
    >
      <Card>
        {/* Header */}
        <div className="flex items-start justify-between mb-4 pb-4 border-b border-white/10">
          <div className="flex items-center gap-3">
            <div className="p-2 rounded-lg bg-solar-orange/20">
              <Clock className="text-solar-orange" size={20} />
            </div>
            <div>
              <p className="text-sm text-star-white/60">Waitlist #{entry.waitlist_id}</p>
              <div className="flex items-center gap-2 mt-1">
                <Clock className="text-solar-orange" size={16} />
                <span className="text-sm font-semibold text-solar-orange">
                  Waiting for a seat
                </span>
              </div>
            </div>
          </div>
        </div>

        {/* Flight Details */}
        {flight ? (
          <div className="space-y-3 mb-4">
            <div>
              <h3 className="text-xl font-bold text-star-white mb-1">
                {flight.origin} → {flight.destination}
              </h3>
              <p className="text-sm text-star-white/60">Flight #{flight.flight_id}</p>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div>
                <p className="text-xs text-star-white/60 mb-1">Departure</p>
                <p className="text-sm text-star-white font-medium">
                  {formatDate(flight.departure_time)}
                </p>
              </div>
              <div>
                <p className="text-xs text-star-white/60 mb-1">Arrival</p>
                <p className="text-sm text-star-white font-medium">
                  {formatDate(flight.arrival_time)}
                </p>
              </div>
            </div>

            <div className="pt-3 border-t border-white/10">
              <div className="flex items-center justify-between">
                <span className="text-sm text-star-white/60">Seat Class</span>
                <div className="flex items-center gap-2">
                  {getSeatClassIcon()}
                  <span className={`text-sm font-semibold ${getSeatClassColor()}`}>
                    {getSeatClassName()}
                  </span>
                </div>
              </div>
            </div>
          </div>
        ) : (
          <div className="mb-4">
            <p className="text-sm text-star-white/60">
              Flight #{entry.flight_id} · {getSeatClassName()}
            </p>
          </div>
        )}

        {/* Joined date */}
        <div className="flex items-center gap-2 text-sm text-star-white/60 mb-4">
          <Calendar size={16} />
          <span>Joined {formatDate(entry.added_at)}</span>
        </div>

        {/* Leave Waitlist Button */}
        <Button
          variant="danger"
          size="sm"
          onClick={() => onLeave(entry.waitlist_id)}
          isLoading={isLeaving}
          className="w-full"
        >
          Leave Waitlist
        </Button>
      </Card>
    </motion.div>
  );
};

// Made with Bob
