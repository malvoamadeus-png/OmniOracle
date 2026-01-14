"use client";

import React, { useState, useEffect } from "react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { supabase } from "@/lib/supabase";
import { useAuth } from "@/lib/AuthContext";
import { Loader2, Trophy, Target, AlertCircle, CheckCircle2, XCircle } from "lucide-react";
import { redirect } from "next/navigation";

interface UserProfile {
  username: string;
  avatar_url: string;
  total_predictions: number;
  correct_predictions: number;
}

interface BetHistory {
  id: string;
  choice: boolean;
  created_at: string;
  predictions: {
    title: string;
    result: boolean | null;
    status: string;
  };
}

export default function ProfilePage() {
  const { user, loading: authLoading } = useAuth();
  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [bets, setBets] = useState<BetHistory[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!authLoading && !user) {
      redirect("/"); // Redirect if not logged in
    }
    if (user) {
      fetchProfileData();
    }
  }, [user, authLoading]);

  const fetchProfileData = async () => {
    if (!user) return;
    try {
      // 1. Fetch Profile Stats
      const { data: profileData, error: profileError } = await supabase
        .from('profiles')
        .select('*')
        .eq('id', user.id)
        .single();
      
      if (profileError) throw profileError;
      setProfile(profileData);

      // 2. Fetch Bet History
      const { data: betsData, error: betsError } = await supabase
        .from('user_bets')
        .select(`
          id,
          choice,
          created_at,
          predictions (
            title,
            result,
            status
          )
        `)
        .eq('user_id', user.id)
        .order('created_at', { ascending: false });

      if (betsError) throw betsError;
      setBets(betsData as any);

    } catch (error) {
      console.error("Error fetching profile:", error);
    } finally {
      setLoading(false);
    }
  };

  if (authLoading || loading) {
    return (
      <div className="flex h-[50vh] items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-gray-400" />
      </div>
    );
  }

  if (!profile) return null;

  const winRate = profile.total_predictions > 0 
    ? ((profile.correct_predictions / profile.total_predictions) * 100).toFixed(1) 
    : "0.0";

  return (
    <div className="container mx-auto p-6 max-w-4xl space-y-8">
      {/* Profile Header */}
      <div className="flex items-center gap-6">
        <Avatar className="h-24 w-24 border-4 border-white shadow-lg">
          <AvatarImage src={profile.avatar_url} />
          <AvatarFallback className="text-2xl">{profile.username?.[0] || "U"}</AvatarFallback>
        </Avatar>
        <div>
          <h1 className="text-3xl font-bold">{profile.username || "Anonymous User"}</h1>
          <p className="text-gray-500">{user?.email}</p>
        </div>
      </div>

      {/* Stats Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Card>
          <CardContent className="pt-6 flex items-center gap-4">
            <div className="p-3 bg-blue-100 rounded-full text-blue-600">
              <Target className="h-6 w-6" />
            </div>
            <div>
              <p className="text-sm font-medium text-gray-500">Total Predictions</p>
              <h3 className="text-2xl font-bold">{profile.total_predictions}</h3>
            </div>
          </CardContent>
        </Card>
        
        <Card>
          <CardContent className="pt-6 flex items-center gap-4">
            <div className="p-3 bg-green-100 rounded-full text-green-600">
              <Trophy className="h-6 w-6" />
            </div>
            <div>
              <p className="text-sm font-medium text-gray-500">Correct Guesses</p>
              <h3 className="text-2xl font-bold">{profile.correct_predictions}</h3>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardContent className="pt-6 flex items-center gap-4">
            <div className="p-3 bg-yellow-100 rounded-full text-yellow-600">
              <span className="text-xl font-bold">%</span>
            </div>
            <div>
              <p className="text-sm font-medium text-gray-500">Win Rate</p>
              <h3 className="text-2xl font-bold">{winRate}%</h3>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Prediction History */}
      <div className="space-y-4">
        <h2 className="text-xl font-bold">Prediction History</h2>
        {bets.length === 0 ? (
          <Card className="p-8 text-center text-gray-500">
            No predictions yet. Go make some bets!
          </Card>
        ) : (
          <div className="grid gap-3">
            {bets.map((bet) => {
              const pred = bet.predictions;
              let statusIcon = <AlertCircle className="h-5 w-5 text-gray-400" />;
              let statusText = "Pending";
              let statusColor = "bg-gray-100 text-gray-600";

              if (pred.status === 'closed') {
                if (pred.result === null) {
                    statusText = "Settling";
                } else if (pred.result === bet.choice) {
                  statusIcon = <CheckCircle2 className="h-5 w-5 text-green-500" />;
                  statusText = "Won";
                  statusColor = "bg-green-100 text-green-700 border-green-200";
                } else {
                  statusIcon = <XCircle className="h-5 w-5 text-red-500" />;
                  statusText = "Lost";
                  statusColor = "bg-red-50 text-red-700 border-red-200";
                }
              } else {
                  statusColor = "bg-blue-50 text-blue-700 border-blue-200";
              }

              return (
                <Card key={bet.id} className="flex items-center justify-between p-4 hover:bg-gray-50 transition-colors">
                  <div className="flex flex-col gap-1">
                    <h3 className="font-medium">{pred.title}</h3>
                    <div className="flex items-center gap-2 text-sm text-gray-500">
                      <span>Your Pick:</span>
                      <span className={`font-bold ${bet.choice ? 'text-green-600' : 'text-red-600'}`}>
                        {bet.choice ? "YES" : "NO"}
                      </span>
                      <span className="text-xs text-gray-400">• {new Date(bet.created_at).toLocaleDateString()}</span>
                    </div>
                  </div>
                  
                  <div className={`flex items-center gap-2 px-3 py-1 rounded-full border text-sm font-medium ${statusColor}`}>
                    {statusIcon}
                    <span>{statusText}</span>
                  </div>
                </Card>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
