"use client";

import { Button } from "@/components/ui/button";
import { useAuth } from "@/lib/AuthContext";
import { Loader2, LogIn, LogOut, User as UserIcon, ChevronRight } from "lucide-react";
import Link from "next/link";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";

export function AuthButton() {
  const { user, loading, signInWithGoogle, signOut } = useAuth();

  if (loading) {
    return (
      <Button variant="outline" disabled className="bg-white/80 backdrop-blur shadow-sm rounded-full">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        Loading...
      </Button>
    );
  }

  if (user) {
    return (
      <div className="flex items-center gap-2">
        <Link href="/profile">
          <div className="group flex items-center gap-3 bg-white/90 backdrop-blur-md shadow-md border border-gray-200 rounded-full pl-1 pr-4 py-1 transition-all hover:bg-gray-50 hover:shadow-lg hover:border-blue-200 cursor-pointer">
            <Avatar className="h-8 w-8 border-2 border-white shadow-sm">
              <AvatarImage src={user.user_metadata.avatar_url} />
              <AvatarFallback className="bg-blue-100 text-blue-600 font-bold">
                {user.email?.[0]?.toUpperCase()}
              </AvatarFallback>
            </Avatar>
            
            <div className="flex flex-col items-start">
              <span className="text-xs font-bold text-gray-800 max-w-[100px] truncate">
                {user.user_metadata.full_name || user.email?.split('@')[0]}
              </span>
              <span className="text-[10px] font-medium text-blue-600 flex items-center group-hover:underline">
                View Profile <ChevronRight className="h-3 w-3 ml-0.5" />
              </span>
            </div>
          </div>
        </Link>

        <Button 
          variant="secondary" 
          size="icon" 
          onClick={() => signOut()}
          className="rounded-full shadow-md bg-white/90 hover:bg-red-50 hover:text-red-600 border border-gray-200 h-10 w-10"
          title="Sign Out"
        >
          <LogOut className="h-4 w-4" />
        </Button>
      </div>
    );
  }

  return (
    <Button 
      onClick={() => signInWithGoogle()} 
      className="rounded-full shadow-lg bg-blue-600 hover:bg-blue-700 text-white px-6 font-semibold transition-transform hover:scale-105"
    >
      <LogIn className="mr-2 h-4 w-4" />
      Login
    </Button>
  );
}
