import { redirect } from "next/navigation"

export default function Home() {
  // There is no separate overview yet, and a landing page that only links onward is
  // one extra click on the way to the thing people came for.
  redirect("/traces")
}
