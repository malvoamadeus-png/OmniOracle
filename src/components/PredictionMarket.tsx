"use client";

import React, { useState, useEffect } from "react";
import { Card, CardContent, CardHeader, CardTitle, CardFooter } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { supabase } from "@/lib/supabase";
import { useAuth } from "@/lib/AuthContext";
import { Loader2, CheckCircle2, XCircle, AlertCircle } from "lucide-react";

interface Prediction {
  id: string;
  title: string;
  description: string;
  status: 'open' | 'closed';
  yes_count: number;
  no_count: number;
  created_at: string;
}

interface UserBet {
  choice: boolean;
}

export function PredictionMarket() {
  const { user } = useAuth();
  const [predictions, setPredictions] = useState<Prediction[]>([]);
  const [userBets, setUserBets] = useState<Record<string, boolean>>({});
  const [loading, setLoading] = useState(true);
  const [votingId, setVotingId] = useState<string | null>(null);

  useEffect(() => {
    fetchPredictions();
  }, [user]);

  const fetchPredictions = async () => {
    try {
      // 1. Fetch open predictions
      const { data: preds, error: predError } = await supabase
        .from('predictions')
        .select('*')
        .eq('status', 'open')
        .order('created_at', { ascending: false });

      if (predError) throw predError;
      setPredictions(preds || []);

      // 2. Fetch user's bets if logged in
      if (user) {
        const { data: bets, error: betError } = await supabase
          .from('user_bets')
          .select('prediction_id, choice')
          .eq('user_id', user.id);

        if (betError) throw betError;

        const betMap: Record<string, boolean> = {};
        bets?.forEach(bet => {
          betMap[bet.prediction_id] = bet.choice;
        });
        setUserBets(betMap);
      } else {
        setUserBets({});
      }
    } catch (error) {
      console.error("Error fetching predictions:", error);
    } finally {
      setLoading(false);
    }
  };

  const handleVote = async (predictionId: string, choice: boolean) => {
    if (!user) return;
    setVotingId(predictionId);

    try {
      const { error } = await supabase
        .from('user_bets')
        .insert({
          user_id: user.id,
          prediction_id: predictionId,
          choice: choice
        });

      if (error) throw error;

      // Update local state optimistic update
      setUserBets(prev => ({ ...prev, [predictionId]: choice }));
      
      // Update local counts optimistic update
      setPredictions(prev => prev.map(p => {
        if (p.id === predictionId) {
          return {
            ...p,
            yes_count: choice ? p.yes_count + 1 : p.yes_count,
            no_count: !choice ? p.no_count + 1 : p.no_count
          };
        }
        return p;
      }));

    } catch (error: any) {
      console.error("Voting failed:", error);
      alert(error.message || "Voting failed");
    } finally {
      setVotingId(null);
    }
  };

  if (loading) {
    return (
        <div className="flex justify-center p-8">
            <Loader2 className="h-8 w-8 animate-spin text-gray-400" />
        </div>
    );
  }

  if (predictions.length === 0) {
    return null; // Or show "No active predictions"
  }

  return (
    <div className="space-y-6">
      <h2 className="text-2xl font-bold tracking-tight">Active Predictions</h2>
      <div className="grid gap-4">
        {predictions.map((pred) => {
          const totalVotes = pred.yes_count + pred.no_count;
          // Calculate percentages with a minimum visual width of 10%
          const yesRawPct = totalVotes === 0 ? 50 : (pred.yes_count / totalVotes) * 100;
          const yesDisplayPct = Math.max(15, Math.min(85, yesRawPct));
          const noDisplayPct = 100 - yesDisplayPct;
          
          const hasVoted = userBets.hasOwnProperty(pred.id);
          const userChoice = userBets[pred.id]; // true=Yes, false=No

          return (
            <Card key={pred.id} className="overflow-hidden border-l-4 border-l-blue-500">
              <CardHeader className="pb-2">
                <CardTitle className="text-lg">{pred.title}</CardTitle>
                {pred.description && (
                    <p className="text-sm text-gray-500">{pred.description}</p>
                )}
              </CardHeader>
              <CardContent className="pb-6">
                <div className="relative h-12 w-full flex rounded-lg overflow-hidden font-bold text-white text-sm shadow-inner bg-gray-100">
                  
                  {/* YES Button/Bar */}
                  <button
                    disabled={!user || hasVoted || votingId === pred.id}
                    onClick={() => handleVote(pred.id, true)}
                    className={`
                      flex items-center justify-start px-4 transition-all duration-500
                      ${hasVoted && userChoice === true ? 'bg-green-600 ring-2 ring-green-400 z-10' : 'bg-emerald-500 hover:bg-emerald-600'}
                      ${(!user || hasVoted) ? 'cursor-default' : 'cursor-pointer'}
                    `}
                    style={{ width: `${yesDisplayPct}%` }}
                  >
                    <span>YES {yesRawPct.toFixed(0)}%</span>
                    {hasVoted && userChoice === true && <CheckCircle2 className="ml-2 h-4 w-4" />}
                  </button>

                  {/* NO Button/Bar */}
                  <button
                    disabled={!user || hasVoted || votingId === pred.id}
                    onClick={() => handleVote(pred.id, false)}
                    className={`
                      flex items-center justify-end px-4 transition-all duration-500
                      ${hasVoted && userChoice === false ? 'bg-red-600 ring-2 ring-red-400 z-10' : 'bg-rose-500 hover:bg-rose-600'}
                      ${(!user || hasVoted) ? 'cursor-default' : 'cursor-pointer'}
                    `}
                    style={{ width: `${noDisplayPct}%` }}
                  >
                    {hasVoted && userChoice === false && <CheckCircle2 className="mr-2 h-4 w-4" />}
                    <span>NO {(100 - yesRawPct).toFixed(0)}%</span>
                  </button>

                  {/* Center Divider/VS */}
                  <div className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 bg-white text-gray-900 rounded-full w-8 h-8 flex items-center justify-center text-xs font-bold shadow-sm z-20 pointer-events-none">
                    VS
                  </div>
                </div>

                {!user && (
                    <div className="mt-2 text-center">
                        <span className="text-xs text-gray-400 flex items-center justify-center gap-1">
                            <AlertCircle className="h-3 w-3" /> Login to vote
                        </span>
                    </div>
                )}
                {hasVoted && (
                    <div className="mt-2 text-center">
                        <span className="text-xs text-gray-500 font-medium">
                            You voted: {userChoice ? "YES" : "NO"}
                        </span>
                    </div>
                )}
              </CardContent>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
