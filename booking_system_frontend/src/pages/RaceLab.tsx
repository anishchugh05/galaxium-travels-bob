import { useState } from 'react';
import { motion } from 'framer-motion';
import { Zap, FlaskConical } from 'lucide-react';
import { resetRaceLab, bookFlight, isErrorResponse } from '../services/api';
import type { RaceLabUser } from '../services/api';

type TileState = 'idle' | 'pending' | 'booked' | 'rejected';

interface RacerTile {
  user: RaceLabUser;
  state: TileState;
  label: string;
}

interface RunStats {
  booked: number;
  rejected: number;
  seatsLeft: number;
}

export const RaceLab = () => {
  const [tiles, setTiles] = useState<RacerTile[]>([]);
  const [stats, setStats] = useState<RunStats | null>(null);
  const [running, setRunning] = useState(false);
  const [flightId, setFlightId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const patchTile = (index: number, patch: Partial<RacerTile>) =>
    setTiles((prev) => {
      const next = [...prev];
      next[index] = { ...next[index], ...patch };
      return next;
    });

  const handleRun = async () => {
    setRunning(true);
    setStats(null);
    setError(null);

    // Step 1: reset backend
    let resetResult;
    try {
      resetResult = await resetRaceLab();
    } catch {
      setError('Failed to reset Race Lab. Is the backend running?');
      setRunning(false);
      return;
    }

    const { flight_id, users } = resetResult;
    setFlightId(flight_id);

    // Step 2: show all tiles as pending
    setTiles(
      users.map((u) => ({ user: u, state: 'pending', label: 'Waiting…' }))
    );

    // Step 3: fire all 20 bookings simultaneously; track results locally
    const localOutcomes: { state: TileState; label: string }[] = users.map(() => ({
      state: 'pending',
      label: '…',
    }));

    await Promise.all(
      users.map((u, i) =>
        bookFlight({
          user_id: u.user_id,
          name: u.name,
          flight_id,
          seat_class: 'economy',
        })
          .then((res) => {
            const outcome = isErrorResponse(res)
              ? { state: 'rejected' as TileState, label: (res as { error: string }).error || 'Rejected' }
              : { state: 'booked' as TileState, label: 'Booked ✓' };
            localOutcomes[i] = outcome;
            patchTile(i, outcome);
          })
          .catch((err: { error?: string } | Error) => {
            const msg =
              (err as { error?: string }).error ||
              (err as Error).message ||
              'Rejected';
            const outcome = { state: 'rejected' as TileState, label: msg };
            localOutcomes[i] = outcome;
            patchTile(i, outcome);
          })
      )
    );

    // Step 4: compute stats
    const booked = localOutcomes.filter((r) => r.state === 'booked').length;
    const rejected = localOutcomes.filter((r) => r.state === 'rejected').length;
    // Seats left = 5 - booked (atomic decrement guarantees max 5 success)
    const seatsLeft = Math.max(0, 5 - booked);

    setStats({ booked, rejected, seatsLeft });
    setRunning(false);
  };

  const tileClass = (state: TileState) => {
    switch (state) {
      case 'booked':
        return 'bg-alien-green/20 border-alien-green text-alien-green';
      case 'rejected':
        return 'bg-solar-orange/20 border-solar-orange text-solar-orange';
      case 'pending':
        return 'bg-white/5 border-white/20 text-star-white/60 animate-pulse';
      default:
        return 'bg-white/5 border-white/10 text-star-white/40';
    }
  };

  const tileStatus = (state: TileState) => {
    if (state === 'booked') return 'Booked';
    if (state === 'rejected') return 'Rejected';
    if (state === 'pending') return 'Waiting…';
    return 'Idle';
  };

  const hasRun = tiles.length > 0;

  return (
    <div className="min-h-screen pt-24 pb-12 px-4">
      <div className="container mx-auto max-w-6xl">
        {/* Page header */}
        <div className="mb-8 flex items-center gap-3">
          <FlaskConical className="text-alien-green" size={32} />
          <div>
            <h1 className="text-3xl font-bold text-star-white">Race Lab</h1>
            <p className="text-star-white/60 mt-1 text-sm">
              Reproduce a concurrency race condition live. 20 users compete for 5 seats simultaneously.
            </p>
          </div>
        </div>

        <div className="flex flex-col lg:flex-row gap-8">
          {/* ── Left / main ── */}
          <div className="flex-1 space-y-6">
            {/* Fire button */}
            <div className="glass-card rounded-xl p-6 border border-white/10">
              <button
                onClick={handleRun}
                disabled={running}
                className={`w-full flex items-center justify-center gap-3 py-4 px-6 rounded-xl font-bold text-lg transition-all
                  ${
                    running
                      ? 'bg-white/10 text-star-white/40 cursor-not-allowed'
                      : 'bg-alien-green hover:bg-alien-green/80 text-space-dark cursor-pointer'
                  }`}
              >
                <Zap size={22} />
                {running
                  ? 'Racing…'
                  : hasRun
                  ? 'Run again'
                  : 'Fire 20 bookings at a 5-seat flight'}
              </button>
              {flightId !== null && (
                <p className="mt-3 text-xs text-star-white/40 text-center">
                  Demo flight ID: {flightId}
                </p>
              )}
              {error && (
                <p className="mt-3 text-sm text-solar-orange text-center">{error}</p>
              )}
            </div>

            {/* 5×4 racer grid */}
            {hasRun && (
              <div>
                <h2 className="text-xs font-semibold text-star-white/50 uppercase tracking-wider mb-3">
                  Racers: {tiles.filter((t) => t.state === 'booked').length} booked /{' '}
                  {tiles.filter((t) => t.state === 'rejected').length} rejected
                </h2>
                <div className="grid grid-cols-4 sm:grid-cols-5 gap-3">
                  {tiles.map((tile, idx) => (
                    <motion.div
                      key={tile.user.user_id}
                      initial={{ opacity: 0.5, scale: 0.95 }}
                      animate={{ opacity: 1, scale: 1 }}
                      transition={{ duration: 0.18, delay: idx * 0.015 }}
                      className={`rounded-lg border px-2 py-3 text-center transition-colors duration-300 ${tileClass(
                        tile.state
                      )}`}
                    >
                      <div className="text-xs font-mono font-semibold truncate">
                        {tile.user.name}
                      </div>
                      <div className="text-xs mt-1 opacity-80">{tileStatus(tile.state)}</div>
                    </motion.div>
                  ))}
                </div>
              </div>
            )}

            {/* Results counters */}
            {stats && (
              <motion.div
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                className="glass-card rounded-xl p-5 border border-white/10 grid grid-cols-3 gap-4 text-center"
              >
                <div>
                  <div className="text-3xl font-bold text-alien-green">{stats.booked}</div>
                  <div className="text-xs text-star-white/60 mt-1">Booked</div>
                </div>
                <div>
                  <div className="text-3xl font-bold text-solar-orange">{stats.rejected}</div>
                  <div className="text-xs text-star-white/60 mt-1">Rejected</div>
                </div>
                <div>
                  <div className="text-3xl font-bold text-star-white">{stats.seatsLeft}</div>
                  <div className="text-xs text-star-white/60 mt-1">Seats left</div>
                </div>
              </motion.div>
            )}
          </div>

          {/* ── Right / side panel ── */}
          <div className="lg:w-80 space-y-4">
            {/* IBM original code panel */}
            <div className="glass-card rounded-xl p-5 border border-solar-orange/40">
              <h3 className="font-bold text-solar-orange mb-3 text-sm uppercase tracking-wider">
                On IBM's original code
              </h3>
              <p className="text-star-white/70 text-sm mb-4">
                Without the atomic seat-decrement fix, all 20 requests read{' '}
                <span className="font-mono text-solar-orange">seats = 5</span> before any write
                commits, then each succeeds independently.
              </p>
              <div className="space-y-2 text-sm">
                <div className="flex justify-between">
                  <span className="text-star-white/60">Booked</span>
                  <span className="font-bold text-solar-orange">20</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-star-white/60">Rejected</span>
                  <span className="font-bold text-alien-green">0</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-star-white/60">Seats showing</span>
                  <span className="font-bold text-solar-orange">3 (ghost value)</span>
                </div>
                <div className="pt-3 mt-3 border-t border-white/10 flex justify-between">
                  <span className="text-star-white/60">Oversold by</span>
                  <span className="font-bold text-red-400">15 seats</span>
                </div>
              </div>
              <p className="mt-4 text-xs text-star-white/40 leading-relaxed">
                The fix uses an atomic compare-and-decrement:{' '}
                <span className="font-mono break-all">
                  WHERE economy_seats_available &gt; 0
                </span>{' '}
                in the same statement that decrements, so only the first 5 writes succeed.
              </p>
            </div>

            {/* How it works */}
            <div className="glass-card rounded-xl p-4 border border-white/10 text-xs text-star-white/50 space-y-2">
              <div className="font-semibold text-star-white/70">How it works</div>
              <p>1. Reset creates one hidden demo flight with exactly 5 economy seats.</p>
              <p>2. All 20 POST /book requests are fired simultaneously via Promise.all.</p>
              <p>3. The backend atomically decrements the counter. Only the first 5 win.</p>
              <p>4. The remaining 15 receive 409 Conflict: no seats available.</p>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

// Made with Bob
