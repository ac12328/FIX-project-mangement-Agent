import { redirect } from "next/navigation";

interface LegacyClosingPageProps {
  params: Promise<{ id: string }>;
}

export default async function LegacyClosingPage({ params }: LegacyClosingPageProps) {
  const { id } = await params;
  redirect(`/projects/${encodeURIComponent(id)}`);
}
