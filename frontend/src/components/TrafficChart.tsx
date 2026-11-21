import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

export interface TrafficPoint {
  time: string
  in_mb: number
  out_mb: number
}

export default function TrafficChart({ points }: { points: TrafficPoint[] }) {
  return (
    <ResponsiveContainer>
      <LineChart data={points}>
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis dataKey="time" minTickGap={24} />
        <YAxis unit=" MB" />
        <Tooltip />
        <Legend />
        <Line type="monotone" dataKey="in_mb" name="入站 MB" stroke="#1677ff" dot={false} />
        <Line type="monotone" dataKey="out_mb" name="出站 MB" stroke="#52c41a" dot={false} />
      </LineChart>
    </ResponsiveContainer>
  )
}
